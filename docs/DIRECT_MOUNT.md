# Directly mounted frontend

The Commerce API serves the complete static frontend: `/`, `/shop`, `/merchant`, and
`/platform`. The browser requests `/api/commerce/*`, `/api/merchant/*`, or
`/api/platform/*`; the backend session adapter dispatches into its existing `/v1/*`
ASGI router. There is no Node server or per-endpoint proxy allowlist in this path.
Group restrictions exclude fixture/protocol administration and keep the open console
limited to evidence reads, operator process metrics, explicitly confirmed tenant Safe Mode changes and exact existing DEAD-command revival. Scenario mutation and protocol administration remain blocked.

## Run locally

Run `npm run build` in `apps/razorsharp-concept`, then
`bash scripts/run_mounted_demo.sh` from the repository root. This starts the existing
API, worker and voice processes. Open `http://localhost:8000/shop` or `/platform`.
Stop an earlier API using port 8000 before starting it. `--api-only` starts only the
API; the worker and voice gateway must already be running for those capabilities.
Rebuild after frontend changes. The API serves the rebuilt files without a restart.

`FRONTEND_DIST` points to the build directory. `BROWSER_DEMO_ENABLED=true` enables
browser session minting only in a backend demo/development profile.
`OPERATOR_DEMO_OPEN_ACCESS=true` enables code-free operator demo entry.
`BROWSER_ORIGIN` and optional comma-separated `BROWSER_ADDITIONAL_ORIGINS` are explicit
trusted browser origins. Do not derive them from untrusted forwarded headers.
The local launcher supplies these demo defaults. The GCE compose file uses its configured
hostnames, and the API Docker image includes the static build. No GKE is required.

The launcher is for an open demo with demo data. It is not production authentication.
Cookies remain HttpOnly and SameSite=Strict; HTTPS origins use Secure cookies. Surface
roles are fixed server-side, existing sessions are checked before dispatch, and backend
capability/ownership checks remain authoritative. An expired session is replaced before
execution; a failed or uncertain mutation is never automatically replayed.

## Events and voice

Checkout recovery and order evidence subscribe to the existing checkout SSE endpoint.
Events trigger a fresh backend read; they never themselves assert payment success.
Native EventSource resumes with Last-Event-ID, and terminal streams close. Recovery
retains a serialized polling fallback and reconciliation, so a stream outage cannot
turn an unknown payment into failure or create a new payment attempt.

The ASGI adapter forwards stream frames and disconnects directly. Caddy flushes frames
immediately. Voice tickets are minted by the backend from the buyer cookie; voice
WebSockets still use the separate voice gateway, with origin and one-use ticket checks.

The GCE wiring is prepared in source. This change does not deploy or restart the live site.
