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
| **Ada** (SM89) | RTX 4090, RTX 6000 Ada, L40(S), L4 | ✅ | Should work — help us confirm |
| **Ampere** (SM80/86) | A100, A10, RTX 3090 | ❌ | Not supported (no native FP8) |

## Tested GPUs

Legend: ✅ validated · 🟡 reported working · ❓ untested · ❌ won't run (default model)

| GPU | Arch | VRAM | Status | Notes |
|---|---|---|:--:|---|
| **H100** (SXM/PCIe) | Hopper | 80 GB | ✅ | Full e2e: web UI + live SIP call + barge-in. TTFA ~0.12–0.16 s |
| **GH200** | Hopper (ARM64) | 96 GB | ✅ | Runs the arm64 build |
| **DGX Spark** | Blackwell (ARM) | 128 GB unified | ❓ | The target desktop box — reports wanted |
| **RTX PRO 6000** | Blackwell | 96 GB | ❓ | FP8-capable — reports wanted |
| **RTX 4090** | Ada | 24 GB | ❓ | FP8-capable — *should* work, not yet confirmed. Tester report wanted |
| **L40 / L40S / L4** | Ada | 48 / 48 / 24 GB | ❓ | FP8-capable — reports wanted |
| **A100 / A10 / RTX 3090** | Ampere | — | ❌ | No native FP8; the default FP8 model won't run natively |

## Report your result 🙏

Ran it on your GPU? Open an issue titled **`[hardware] <your GPU>`** with:

- GPU + driver version
- Did `setup.sh` finish? (and roughly how long)
- TTFA / round-trip latency you saw
- Anything that broke

We'll add your row — this table is community-built. Thanks for helping map it out.
