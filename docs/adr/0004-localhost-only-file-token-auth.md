# Localhost-only daemon, file-based bearer token

By default the daemon binds to loopback only — `127.0.0.1`, `localhost`, or `::1` — and refuses non-loopback hosts unless `--unsafe-host` is explicitly supplied. It authenticates with a bearer token generated on first start at `~/.riview/token` (mode 0600). The token is embedded in valid per-session review pages so the page's own JS can POST reviews back; cross-origin requests from other localhost ports fail without the token. The host gate and the token are both enforced server-side.

Alternatives considered: no auth at all (any local process or browser tab could submit reviews to any session); OS keychain (adds Keychain/secret-service/wincred dependencies — see ADR-0001 on stdlib-only); a unix socket instead of TCP (browsers can't open SSE/POST over unix sockets without a relay). A file token at a well-known path is the minimum that keeps a casual `curl http://localhost:7891/...` from another browser tab out, while still letting the rendered page itself work without any handshake.

The model is "one user, one machine, multiple agents on that machine". It is not a defence against a hostile local user or a multi-tenant box.
