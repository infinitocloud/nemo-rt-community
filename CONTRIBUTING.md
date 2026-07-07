# Contributing to Nemo-RT Community

Thanks for being here. This is a small, focused project — a real-time, on-prem
voice AI core — and contributions of all sizes are welcome.

## Where things go

- **Questions, ideas, "how do I…", show-and-tell** → [Discussions](../../discussions).
- **Bugs and concrete feature requests** → [Issues](../../issues) (use the templates).
- **Security vulnerabilities** → **do not** open a public issue. See [SECURITY.md](SECURITY.md).

## Running it locally

`./setup.sh` brings up a full agent on a fresh NVIDIA box in one command — see the
[README](README.md#quick-start--one-command). Every env var is documented in
[`.env.example`](.env.example); `docker-compose.yml` and `setup.sh` show the full run.

## Pull requests

- Keep PRs **small and focused** — one change, one reason.
- Describe **what** changed and **why**, and how you tested it (GPU + arch help a lot).
- Match the surrounding style; don't reformat unrelated code.
- If it changes behavior, update [`README.md`](README.md).
- Don't regress the validated gotchas: `LLM_FREQ_PENALTY=0.0` on Blackwell GPUs
  (RTX PRO 6000), and the OMP/MKL thread cap on high-core ARM hosts (GH200, DGX
  Spark) — both already applied in `start.sh` / `.env.example`.

## Licensing

This project is **Apache 2.0**. By contributing, you agree your contributions are
licensed under the same terms (inbound = outbound). Don't submit code you don't
have the right to contribute.

## Tone

Be kind and concrete. We're all running models on our own boxes here.
