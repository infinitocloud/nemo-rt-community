#!/usr/bin/env bash
# =============================================================================
#  Nemo-RT Community — production boot
#  Brings up vLLM (:8002) + web_app (:8000) and waits until /health is ready.
#  Usage:
#      ssh root@<IP> -p <PORT> -i ~/.ssh/id_ed25519
#      bash /workspace/start.sh
#  Or set this as RunPod "Container Start Command" so the pod boots on its own.
# =============================================================================
set -e
set -a; source /workspace/nemo_rt/.env; set +a
export HF_HOME=/workspace/hf_cache PYTHONUNBUFFERED=1
cd /workspace/nemo_rt
mkdir -p /workspace/logs

log() { echo "[$(date +%T)] $*"; }

# Image layout auto-detect: hand-built GHCR venvs (/opt/venv-*) OR self-contained
# single-python (NeMo+web+vLLM in the base interpreter). Keeps ONE start.sh working
# with both the legacy :2.0 image and the self-contained multi-arch build.
VLLM_BIN=$([ -x /opt/venv-vllm/bin/vllm ] && echo /opt/venv-vllm/bin/vllm || echo vllm)
PY_BIN=$([ -x /opt/venv-web/bin/python ] && echo /opt/venv-web/bin/python || echo python)
PIP_BIN=$([ -x /opt/venv-web/bin/pip ] && echo /opt/venv-web/bin/pip || echo pip)

log "cleaning old processes..."
pkill -9 -f "vllm serve" 2>/dev/null || true
pkill -9 -f "web_app.py" 2>/dev/null || true
sleep 2

# -----------------------------------------------------------------------------
# Ensure deps that are NOT baked into the GHCR image but are required by the
# current web_app are present. The container disk gets reset on every pod
# Stop/Start, so install-or-skip here keeps every boot self-healing.
# Remove once we rebuild the Docker image with these baked in.
# -----------------------------------------------------------------------------
if ! $PY_BIN -c "import bcrypt, slowapi" 2>/dev/null; then
  log "installing missing Python deps (bcrypt, slowapi)..."
  $PIP_BIN install --no-cache-dir bcrypt slowapi > /workspace/logs/pip.log 2>&1
fi

# -----------------------------------------------------------------------------
# Hot-patch vLLM dense GEMM on Hopper (SM90).
# Bug: flashinfer 0.6.8 doesn't ship the fp8_blockscale_gemm_sm90 cubin → vLLM
# crashes at the first forward of an FP8 model on Hopper. Fix: force run_deepgemm
# (DeepGEMM, already compiled in image :2.0), bypassing the torch.cond that
# routed to flashinfer for M<32.
# -----------------------------------------------------------------------------
VLLM_GEMM_PATCH=/usr/local/lib/python3.12/dist-packages/vllm/model_executor/kernels/linear/scaled_mm/flashinfer.py
if ! grep -q "HOTPATCH" "$VLLM_GEMM_PATCH" 2>/dev/null; then
  log "applying vLLM dense-GEMM hot-patch..."
  python3 - <<PYEOF
p = "$VLLM_GEMM_PATCH"
src = open(p).read()
old = "    if envs.VLLM_BATCH_INVARIANT:\n        return run_deepgemm(input, weight, weight_scale)\n"
new = "    # HOTPATCH: force run_deepgemm on Hopper (flashinfer cubin SM90 missing). Was: if envs.VLLM_BATCH_INVARIANT:\n    return run_deepgemm(input, weight, weight_scale)\n"
if old in src:
    open(p, "w").write(src.replace(old, new, 1))
    print("  hot-patch applied")
else:
    print("  WARN: marker not found (vLLM != 0.21.0?)")
PYEOF
else
  log "vLLM hot-patch already in place"
fi

log "starting vLLM on :8002..."
nohup $VLLM_BIN serve "$LLM_MODEL" \
  --host 0.0.0.0 --port 8002 --tensor-parallel-size 1 \
  --max-model-len 32768 --gpu-memory-utilization 0.75 --max-num-seqs 256 \
  > /workspace/logs/vllm.log 2>&1 </dev/null &
disown

log "waiting for vLLM (compile + load, ~2-3 min)..."
for i in $(seq 1 60); do
  curl -fs http://localhost:8002/v1/models -m 4 2>/dev/null | grep -q object && { log "  vLLM UP"; break; }
  sleep 10
done

log "starting web_app on :8000..."
# OMP/MKL thread cap: on high-core hosts (GH200 64 vCPUs, big servers) the NeMo
# Conformer load triggers an OMP thread explosion (200+ threads) that DEADLOCKS the
# STT instantiation (web_app stuck at "[STT] Loading ..."). Capping the CPU thread
# pools fixes it. Validated on GH200/aarch64 2026-06-20.
nohup env CUDA_MODULE_LOADING=EAGER OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4 \
  $PY_BIN -u web_app.py > /workspace/logs/web.log 2>&1 </dev/null &
disown

# /health no longer accepts ?key= in the URL (SSRF/log-leak protection).
# Authenticated client: send the API key in the Authorization header.
log "waiting for web_app 'ready'..."
K=$(grep "^API_KEY=" /workspace/nemo_rt/.env | cut -d= -f2)
READY=0
for i in $(seq 1 30); do
  if curl -fs -H "Authorization: Bearer $K" http://localhost:8000/health -m 4 2>/dev/null | grep -q '"ready":true'; then
    READY=1
    break
  fi
  sleep 10
done

echo ""
if [ "$READY" = "1" ]; then
  echo "✅ READY"
else
  echo "⚠️  still warming up — tail /workspace/logs/web.log"
fi
echo "URL:     https://${RUNPOD_POD_ID:-<PODID>}-8000.proxy.runpod.net/"
echo "Public:  https://live-nemo.infinitocloud.com/"
echo "API_KEY: $K"
