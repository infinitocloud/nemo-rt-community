# Hardware compatibility

Nemo-RT's default model is **Qwen3-8B-FP8**, so it needs a GPU with **native FP8
tensor cores** — i.e. **Ada, Hopper, or Blackwell**. Ampere (A100, A10, RTX 3090)
has **no native FP8** and is not a target for the default model.

**VRAM:** the default model needs **~12 GB**, so any FP8-capable card with ≥16 GB is
comfortable. (Run other sizes via the `LLM_MODEL` env var.)

## Architecture support

| Architecture | Example GPUs | Native FP8 | Default model |
|---|---|:--:|---|
| **Blackwell** (SM100+) | B200, RTX PRO 6000, RTX 5090 | ✅ | Expected |
| **Hopper** (SM90) | H100, H200, GH200 | ✅ | ✅ Validated |
| **Ada** (SM89) | RTX 4090, RTX 6000 Ada, L40(S), L4 | ✅ | ✅ Validated on RTX 4090 |
| **Ampere** (SM80/86) | A100, A10, RTX 3090 | ❌ | Not supported (no native FP8) |

## Tested GPUs

Legend: ✅ validated · 🟡 reported working · ❓ untested · ❌ won't run (default model)

| GPU | Arch | VRAM | Status | Notes |
|---|---|---|:--:|---|
| **H100** (SXM/PCIe) | Hopper | 80 GB | ✅ | Full e2e: web UI + live SIP call + barge-in. TTFA ~0.12–0.16 s |
| **GH200** | Hopper (ARM64) | 96 GB | ✅ | Runs the arm64 build |
| **DGX Spark** | Blackwell (ARM) | 128 GB unified | ❓ | The target desktop box — reports wanted |
| **RTX PRO 6000** | Blackwell | 96 GB | ❓ | FP8-capable — reports wanted |
| **RTX 4090** | Ada | 24 GB | ✅ | Validated 2026-07-08: full stack fits in ~21.5/24 GB. Live voice TTFA **0.17–0.59 s**. LLM 52 tok/s single, 32 concurrent OK. Tight VRAM → dev / small deployments ([details](#measured--rtx-4090-2026-07-08)) |
| **L40 / L40S / L4** | Ada | 48 / 48 / 24 GB | ❓ | FP8-capable — reports wanted |
| **A100 / A10 / RTX 3090** | Ampere | — | ❌ | No native FP8; the default FP8 model won't run natively |

## Measured — RTX 4090 (2026-07-08)

A single **consumer RTX 4090** (24 GB, driver 580 / CUDA 13.0), default `Qwen3-8B-FP8`
+ full NeMo STT/TTS, everything on the one card:

- **It fits:** the whole stack uses **~21.5 / 24 GB** (vLLM at `--gpu-memory-utilization 0.75`
  + NeMo STT/TTS). ~2.5 GB headroom.
- **Live voice (browser, Spanish STT → LLM → TTS):** TTFA **0.17 s** ("hola"), **0.27 s**
  (short reply), **0.59 s** (longer answer) — sub-second, real-time.
- **LLM throughput:** 52 tok/s single-stream (well above real-time speech); **32 concurrent**
  short requests handled with **0 errors** (~1,745 tok/s aggregate). vLLM KV cache ~50k tokens.
- **Takeaway:** excellent for **development, testing and small deployments**. VRAM headroom is
  tight, so for high concurrent-call volume the 128 GB **DGX Spark** remains the better
  production desktop. (Lower `--max-model-len` to trade context length for more concurrent sessions.)

## Report your result 🙏

Ran it on your GPU? Open an issue titled **`[hardware] <your GPU>`** with:

- GPU + driver version
- Did `setup.sh` finish? (and roughly how long)
- TTFA / round-trip latency you saw
- Anything that broke

We'll add your row — this table is community-built. Thanks for helping map it out.
