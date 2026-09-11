# The manual work, and nothing else

Everything is done. The demo is live on your own domain, over HTTPS, with both copilots
answering.

**<https://razorsharp.vedanttyagi.tech>**

Two stopgap hostnames still answer the same site — `34.93.90.117.nip.io` and
`34-93-90-117.sslip.io` — and you should hand out the domain rather than either of them.
Both wildcard-DNS services are on filter lists several ad blockers ship by default: the page
loads and every request it makes is refused `ERR_BLOCKED_BY_CLIENT`, which looks exactly
like a broken backend and is not one. It happened here in testing. Your domain is not on
those lists, which is the whole reason it matters.

---

## I was wrong about the orange cloud

I told you to set the record to grey cloud, because Cloudflare's proxy usually intercepts
the HTTP-01 challenge Caddy needs to get a certificate. It did not: Cloudflare passed the
challenge through to the origin, the authorisation came back valid, and the certificate was
issued with the proxy left on. Keep it on — you get Cloudflare's protection and end-to-end
TLS, with their certificate at the edge and Caddy's own on the origin leg.

Nothing for you to change.

---

## What is left for you

**1. Try a payment.** Open the shop, add something, go through Razorpay Checkout with a
test card. Razorpay is in **TEST mode**; no real money moves. This is yours because typing
into Razorpay's iframe is blocked for my tooling, not because anything is unfinished --
everything up to it is verified, and the browser callback is recorded as a callback and
never as a capture.

**2. Decide about voice.** It works end to end on the deployed site. It runs on Vertex AI,
which bills per request, unlike the rest of the demo which is the VM and nothing else. Say
the word and I will disable the route for judging.

That is the whole list.

---

## Running cost

One `e2-standard-2` in `asia-south1`, a 50 GB disk and an ephemeral address: roughly **$15
for a judging week** against your trial credits. Stop it between demos and you pay for the
disk alone:

```bash
gcloud compute instances stop razorsharp-demo --zone asia-south1-a
gcloud compute instances start razorsharp-demo --zone asia-south1-a
```

**One caution, and it is the only way to break this now.** A stop-then-start hands out a
new external address, and your A record would still point at the old one. Either update the
record afterwards, or reserve the address once -- about $3.60 a month -- and never think
about it again:

```bash
gcloud compute addresses create razorsharp-ip --region asia-south1
```

---

## Every name, so nothing reads as scaffolding

| | |
| --- | --- |
| Domain | `razorsharp.vedanttyagi.tech` |
| GCP project | `RazorSharp` (was "My First Project") |
| Instance and disk | `razorsharp-demo` |
| Firewall rule | `razorsharp-web` |
| Tree on the machine | `/opt/razorsharp` |
| Containers | `razorsharp-api-1`, `razorsharp-web-1`, `razorsharp-voice-1`, `razorsharp-worker-1`, `razorsharp-db-1`, `razorsharp-caddy-1` |
| Images | `razorsharp/commerce-api`, `razorsharp/web`, `razorsharp/voice-gateway`, `razorsharp/action-executor` |
| Volumes | `razorsharp_pgdata`, `razorsharp_caddy_data`, `razorsharp_caddy_config` |

`caddy:2-alpine` and `postgres:17-bookworm` keep their own names; they are not ours to
rename.
