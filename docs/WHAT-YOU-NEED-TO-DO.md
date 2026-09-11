# The manual work, and nothing else

Everything that could be done without your accounts is done. The demo is deployed, public
and answering over HTTPS. What is left needs a login only you have.

**Live now:**

- <https://34.93.90.117.nip.io>
- <https://34-93-90-117.sslip.io> — the same site on a second hostname

Both carry a real certificate. Both are stopgaps, and item 1 is why.

---

## 1. Point your domain at the machine — 5 minutes, and it is the only one that matters

In Cloudflare DNS for `vedanttyagi.tech`, add:

| Type | Name | Content | Proxy |
| --- | --- | --- | --- |
| A | `demo` (or `@`) | `34.93.90.117` | **DNS only — grey cloud** |

Grey cloud, not orange. Caddy obtains the certificate itself through an HTTP-01 challenge,
and Cloudflare's proxy intercepts that challenge and breaks it. Turn the proxy on later if
you want it, after the certificate exists.

**Why this is not optional.** `nip.io` and `sslip.io` map any address-shaped hostname to
that address, which is what let this have TLS today with no DNS record. Both are also on
the filter lists several ad blockers ship by default. The page loads and every request the
page makes is refused `ERR_BLOCKED_BY_CLIENT` — which looks exactly like a broken backend
and is not one. It happened in testing here. A judge with uBlock would see a storefront
where nothing works, and would have no reason to think the block was theirs.

Tell me when the record is in and I will point the deployment at it — one line in
`infra/gce/.env` and a restart of the proxy; the certificate follows on its own.

---

## 2. Try a payment yourself — I cannot

Open the shop, add something, and go through Razorpay Checkout with a test card. Razorpay
is in **TEST mode**; no real money moves.

This needs you because typing into Razorpay's iframe is blocked for my tooling, not because
anything is unfinished. Everything up to it is verified: the checkout is created, the
approval is bound to the exact bill, and the browser callback is recorded as a callback and
never as a capture.

---

## 3. Decide about voice — it costs money per use

Voice is deployed and reachable. It runs on Vertex AI, which bills per request, unlike the
rest of the demo which is the VM and nothing else. If you would rather it stayed off during
judging, say so and I will disable the route.

---

## What I could not do, and why

`gcloud projects add-iam-policy-binding` is blocked in my environment, so you ran the Vertex
grant yourself. It is still in place — I checked, the call returns 200. Nothing further is
needed there.

---

## Running cost

One `e2-standard-2` in `asia-south1`, a 50 GB disk, and an ephemeral IP: roughly **$15 for a
judging week**, against your trial credits. Stop it between demos and you pay for the disk
alone:

```bash
gcloud compute instances stop razorsharp-demo --zone asia-south1-a
gcloud compute instances start razorsharp-demo --zone asia-south1-a
```

One caution: a stop-then-start hands out a **new external IP**, which invalidates both
stopgap hostnames and your A record. Once item 1 is done, updating the record is the only
thing to fix. If you would rather that never happened, reserve the address — about $3.60 a
month:

```bash
gcloud compute addresses create razorsharp-ip --region asia-south1
```
