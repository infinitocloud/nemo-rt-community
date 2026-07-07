---
name: Bug report
about: Something doesn't work as expected
title: "[bug] "
labels: bug
---

**What happened**
A clear description of the bug.

**What you expected**
What you expected to happen instead.

**Steps to reproduce**
1.
2.
3.

**Environment** (this matters a lot for voice/GPU issues)
- GPU + VRAM: (e.g. DGX Spark 128GB, H100 80GB, RTX 6000…)
- Architecture: (x86_64 / aarch64)
- NVIDIA driver version:
- `LLM_MODEL` (if not the default):
- Installed via: (`setup.sh` / `docker-compose` / manual `docker run`)

**Logs**
Relevant output from `docker logs` / `web.log` / vLLM (trim secrets — never paste your `API_KEY` or `HF_TOKEN`).

**Anything else**
Screenshots, audio notes, etc.
