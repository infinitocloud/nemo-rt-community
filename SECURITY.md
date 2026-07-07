# Security Policy

Nemo-RT Community runs on your own hardware and processes voice and call data, so
we take security seriously.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately, either:

- Via GitHub's **[Report a vulnerability](../../security/advisories/new)** (private
  security advisory), or
- By email to **yan@infinitocloud.com** with the subject `SECURITY: nemo-rt`.

Please include what you found, how to reproduce it, and the impact. We aim to
acknowledge within a few business days and will keep you updated on the fix.

## Scope notes

- The default deployment is **single-tenant and meant to run inside your own
  network**. `API_KEY` must be set in production — an empty key disables auth and
  is for local development only.
- The browser microphone requires **HTTPS**; put the app behind a TLS reverse
  proxy. Don't expose port 8000 raw to the public internet.

Thank you for helping keep self-hosted voice AI safe.
