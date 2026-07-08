#!/usr/bin/env bash
# =============================================================================
#  Nemo-RT Community — one-shot setup
#  Real-time voice AI (VAD -> STT -> LLM -> TTS) on your own NVIDIA GPU.
#  Single-tenant · bilingual ES/EN · on-premise · no per-minute fees.
#
#  Validated 2026-06-19 from a BARE box (no driver, no docker):
#    Ubuntu 24.04 LTS · x86_64 · 1x NVIDIA H100 80GB  ->  working agent in ~10 min
#    TTFA 0.19-0.22s · sub-second round-trip.
#  Also expected to work on: H200 / Blackwell / RTX PRO 6000 / DGX Spark (ARM).
#  On DGX Spark / Lambda-Stack images the driver is preinstalled -> Step 1 self-skips.
#
#  REQUIREMENTS:
#    - GPU: FP8-capable NVIDIA (Hopper SM90 / Ada SM89 / Blackwell SM100+). ~12GB VRAM
#      for the default Qwen3-8B-FP8 (set a different LLM_MODEL for other sizes).
#    - Driver: >= 580 recommended (image ships torch cu128 / CUDA 12.8 + CUDA 13.0
#      toolkit). Validated on 610.43.02. Step 1 installs the latest cuda-drivers,
#      which always satisfies this — the minimum only matters if you keep an OLD
#      preinstalled driver (then ensure >= 580, or let Step 1 upgrade it).
#    - Disk: ~40GB free (image ~14GB compressed / ~30GB unpacked + model cache
#      ~10GB). First-run download is ~24GB total; later restarts reuse the caches.
#
#  Usage:
#      chmod +x setup.sh && ./setup.sh
#  Re-running is safe: every step detects-and-skips if already done.
#  (If it installs the GPU driver, it will ask you to reboot and re-run once.)
# =============================================================================
set -euo pipefail

# --- colors / helpers --------------------------------------------------------
G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[0;34m'; N='\033[0m'
log()  { echo -e "${B}[setup]${N} $*"; }
ok()   { echo -e "${G}  ok${N}  $*"; }
warn() { echo -e "${Y}  !!${N}  $*"; }
die()  { echo -e "${R}  xx  $*${N}"; exit 1; }
ask()  { local v; read -rp "$(echo -e "${Y}?${N} $1")" v; echo "$v"; }

# --- config (override via env: IMAGE=... ./setup.sh) -------------------------
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${IMAGE:-ghcr.io/infinitocloud/nemo-rt-community:2.0}"   # runtime deps baked (PyTorch/vLLM/NeMo); the app code is mounted from this repo
CONTAINER="${CONTAINER:-nemo-rt-community}"
HF_CACHE="${HF_CACHE:-$HOME/.cache/nemo-rt/hf}"           # persists the LLM weights between restarts (~10GB)
ROOT_CACHE="${ROOT_CACHE:-$HOME/.cache/nemo-rt/root}"     # persists NeMo models + vLLM compile cache -> ~30s restarts
PORT_WEB="${PORT_WEB:-8000}"
PORT_VLLM="${PORT_VLLM:-8002}"
SSL_TUNNEL="${SSL_TUNNEL:-0}"   # SSL_TUNNEL=1 -> free instant HTTPS via Cloudflare quick tunnel (the browser mic needs HTTPS)

echo -e "${G}=== Nemo-RT Community setup ===${N}"
log "repo : $REPO_DIR"
log "image: $IMAGE"

# --- Step 0: OS / arch sanity ------------------------------------------------
log "Step 0/7  OS & architecture"
ARCH="$(uname -m)"
. /etc/os-release 2>/dev/null || true
[ "$(uname -s)" = "Linux" ] || die "This installer is Linux-only."
[ "${ID:-}" = "ubuntu" ] || warn "Not Ubuntu (${ID:-unknown}); driver/docker steps may need adjusting."
case "$ARCH" in
  x86_64)  CUDA_ARCH="x86_64" ;;
  aarch64) CUDA_ARCH="sbsa"; warn "ARM (aarch64, e.g. DGX Spark): the image must be arm64-built — see README cross-arch notes." ;;
  *) die "Unsupported architecture: $ARCH" ;;
esac
ok "Ubuntu ${VERSION_ID:-?} · $ARCH"

# --- Step 1: NVIDIA driver ---------------------------------------------------
log "Step 1/7  NVIDIA driver"
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  ok "$(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader | head -1)"
else
  warn "No working NVIDIA driver. Installing from NVIDIA's CUDA repo..."
  lspci 2>/dev/null | grep -qiE "nvidia" || die "No NVIDIA GPU visible via lspci."
  REPO="ubuntu$(echo "${VERSION_ID:-24.04}" | tr -d '.')"   # e.g. ubuntu2404
  ( cd /tmp
    wget -q "https://developer.download.nvidia.com/compute/cuda/repos/${REPO}/${CUDA_ARCH}/cuda-keyring_1.1-1_all.deb" -O cuda-keyring.deb \
      || die "Could not fetch cuda-keyring for ${REPO}/${CUDA_ARCH}. Install the driver manually and re-run."
    sudo dpkg -i cuda-keyring.deb >/dev/null 2>&1 )
  sudo apt-get update -qq
  log "  installing cuda-drivers (a few hundred MB)..."
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cuda-drivers
  echo
  echo -e "${Y}=============================================================${N}"
  echo -e "${Y}  Driver installed. A REBOOT is required to load the module.${N}"
  echo -e "${Y}    sudo reboot${N}"
  echo -e "${Y}  Then re-run this script:  ./setup.sh${N}"
  echo -e "${Y}=============================================================${N}"
  exit 0
fi

# --- Step 2: Docker ----------------------------------------------------------
log "Step 2/7  Docker"
if command -v docker >/dev/null 2>&1; then
  ok "$(docker --version | awk '{print $1, $3}' | tr -d ,)"
else
  warn "Installing Docker (official convenience script)..."
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sudo sh /tmp/get-docker.sh >/dev/null 2>&1
  sudo usermod -aG docker "$USER" || true
  ok "Docker installed"
fi

# --- Step 3: NVIDIA Container Toolkit (GPU access inside containers) ---------
log "Step 3/7  NVIDIA Container Toolkit"
if dpkg -l nvidia-container-toolkit >/dev/null 2>&1; then
  ok "toolkit present"
else
  warn "Installing nvidia-container-toolkit..."
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nvidia-container-toolkit
  sudo nvidia-ctk runtime configure --runtime=docker >/dev/null 2>&1
  sudo systemctl restart docker
  ok "toolkit installed + docker runtime configured"
fi
log "  verifying GPU is visible inside Docker..."
if sudo docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu24.04 nvidia-smi >/dev/null 2>&1; then
  ok "GPU reachable from containers"
else
  die "GPU not reachable from Docker. Try: sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
fi

# --- Step 4: Configuration (.env) -------------------------------------------
log "Step 4/7  Configuration (.env)"
ENV_FILE="$REPO_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
  [ -f "$REPO_DIR/.env.example" ] && cp "$REPO_DIR/.env.example" "$ENV_FILE" || touch "$ENV_FILE"
fi
get(){ grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }
set_kv(){ if grep -qE "^$1=" "$ENV_FILE"; then sed -i "s#^$1=.*#$1=$2#" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi; }

# HF_TOKEN is optional: the default Qwen/Qwen3-8B-FP8 is a PUBLIC model and downloads
# anonymously. Set HF_TOKEN in .env only for a gated model or to avoid HF rate limits.
[ -n "$(get API_KEY)" ]  || set_kv API_KEY  "sk-$(openssl rand -hex 16 2>/dev/null || head -c16 /dev/urandom | xxd -p)"
# vLLM runs *inside* this same container (launched by start.sh), so the app talks to it on localhost:
set_kv LLM_BASE_URL "http://127.0.0.1:${PORT_VLLM}/v1"
[ -n "$(get LLM_MODEL)" ] || set_kv LLM_MODEL "Qwen/Qwen3-8B-FP8"
ok ".env ready (API_KEY=$(get API_KEY))"

# --- Step 5: Pull image ------------------------------------------------------
log "Step 5/7  Runtime image (deps baked: PyTorch + vLLM + NeMo)"
if sudo docker image inspect "$IMAGE" >/dev/null 2>&1; then
  ok "image already present"
elif [ "$ARCH" = "aarch64" ]; then
  # ARM (GH200 / DGX Spark): pull the prebuilt arm64 image; if unavailable, build it
  # natively from the Dockerfile (one-time, slow) — self-contained single-python build.
  ARM_IMAGE="ghcr.io/infinitocloud/nemo-rt-community:2.0-arm64"
  if sudo docker pull "$ARM_IMAGE" 2>/dev/null; then
    sudo docker tag "$ARM_IMAGE" "$IMAGE"; ok "pulled prebuilt arm64 image"
  else
    warn "no prebuilt arm64 image reachable — building natively from Dockerfile (one-time, several min)..."
    sudo docker build -t "$IMAGE" "$REPO_DIR" || die "arm64 build failed — inspect which dep lacks an aarch64 wheel."
    ok "image built (arm64)"
  fi
else
  sudo docker pull "$IMAGE" || die "Could not pull $IMAGE. If it is private, 'docker login ghcr.io' first, or set IMAGE=<your image>."
  ok "image pulled"
fi

# --- Step 6: Launch ----------------------------------------------------------
log "Step 6/7  Launching the voice agent"
mkdir -p "$HF_CACHE" "$ROOT_CACHE"
sudo docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
# IMPORTANT — keep-alive: start.sh backgrounds vLLM + web_app and then exits.
# In a plain `docker run` the container would stop the instant start.sh returns,
# killing both services. We append `tail -f /dev/null` to keep PID 1 alive.
# The two cache mounts persist model weights + vLLM compile cache => ~30s restarts
# instead of re-downloading ~10GB and recompiling kernels every time.
sudo docker run -d --gpus all --name "$CONTAINER" --restart unless-stopped \
  -p ${PORT_WEB}:8000 -p ${PORT_VLLM}:8002 \
  -v "$REPO_DIR":/workspace/nemo_rt \
  -v "$HF_CACHE":/workspace/hf_cache \
  -v "$ROOT_CACHE":/root/.cache \
  "$IMAGE" \
  bash -c "bash /workspace/nemo_rt/start.sh; tail -f /dev/null"
ok "container started"

# --- Step 7: Wait for ready --------------------------------------------------
log "Step 7/7  Loading models (first run downloads ~10GB + compiles vLLM, several min; later runs ~30s)..."
KEY="$(get API_KEY)"; READY=0
for i in $(seq 1 120); do
  if sudo docker exec "$CONTAINER" curl -fs -H "Authorization: Bearer $KEY" http://localhost:8000/health -m 4 2>/dev/null | grep -q '"ready":true'; then
    READY=1; break
  fi
  sleep 10
done
echo
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ "$READY" = 1 ]; then
  # Optional free public HTTPS (the browser mic needs HTTPS). Off by default to keep
  # everything local; SSL_TUNNEL=1 opts in. Audio then transits Cloudflare's edge.
  TUNNEL_URL=""
  if [ "$SSL_TUNNEL" = "1" ]; then
    log "Setting up a free HTTPS link (Cloudflare quick tunnel)..."
    if ! command -v cloudflared >/dev/null 2>&1; then
      CF_ARCH="amd64"; [ "$ARCH" = "aarch64" ] && CF_ARCH="arm64"
      sudo wget -q "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" \
        -O /usr/local/bin/cloudflared && sudo chmod +x /usr/local/bin/cloudflared
    fi
    if command -v cloudflared >/dev/null 2>&1; then
      nohup cloudflared tunnel --config /dev/null --protocol http2 --url "http://localhost:${PORT_WEB}" \
        >/tmp/nemo-tunnel.log 2>&1 &
      for _ in $(seq 1 20); do
        TUNNEL_URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/nemo-tunnel.log 2>/dev/null | head -1 || true)"
        [ -n "$TUNNEL_URL" ] && break || true
        sleep 1
      done
    fi
  fi
  echo -e "${G}=============================================================${N}"
  echo -e "${G}  Nemo-RT Community is READY${N}"
  echo -e "${G}=============================================================${N}"
  if [ -n "$TUNNEL_URL" ]; then
    echo -e "  ${G}🎤 Talk now (HTTPS, mic works):${N}  ${TUNNEL_URL}/"
    echo -e "      API key: ${KEY}"
    echo -e "      (free Cloudflare quick tunnel — ephemeral URL; audio transits Cloudflare)"
  else
    echo -e "  Web UI : http://${IP:-localhost}:${PORT_WEB}/"
    echo -e "  API key: ${KEY}"
  fi
  echo
  echo -e "  ${Y}Mic note:${N} the browser microphone needs HTTPS (or localhost). Easiest:"
  echo -e "      • Free instant HTTPS:  ${G}SSL_TUNNEL=1 ./setup.sh${N}   (Cloudflare quick tunnel)"
  echo -e "      • Fully local:         ssh -L ${PORT_WEB}:localhost:${PORT_WEB} user@${IP:-SERVER}  ->  http://localhost:${PORT_WEB}"
  echo -e "      • Production:          TLS reverse proxy (nginx/Caddy) on your domain"
  echo
  echo -e "  Logs : sudo docker logs -f ${CONTAINER}"
  echo -e "  Stop : sudo docker rm -f ${CONTAINER}"
else
  warn "Still warming up after ~20 min. Watch the log:"
  echo -e "      sudo docker exec ${CONTAINER} tail -f /workspace/logs/web.log"
fi
