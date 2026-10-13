# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| `main` | ✅ |

## Reporting a vulnerability

Please report security issues **privately** — open a
[private security advisory](https://github.com/yyd330/seconded/security/advisories/new)
rather than a public issue. I aim to respond within a week.

## Important design context

Seconded is built for **your own trusted LAN** and ships with **no authentication by
default**. That is intentional for the home/self-hosted case, but it means:

- The server binds `0.0.0.0` — anyone who can reach the port can record, browse
  sessions, and download transcripts.
- If others can reach your network, set an access token:
  `python server.py --auth-token "$(openssl rand -hex 16)"` (or `SECONDED_TOKEN`).
- **Do not port-forward Seconded to the internet.** For remote access use a tunnel
  (WireGuard, Tailscale, or an authenticating reverse proxy).
- The HTTPS certificate is a per-install local CA (`/rootca`). Install it only on
  devices you own and trust.
- Audio, transcripts and summaries never leave your machine — ASR runs locally and the
  summary backend is your own local LLM endpoint.
