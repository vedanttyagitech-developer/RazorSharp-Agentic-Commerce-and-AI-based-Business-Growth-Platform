# The judging demo, on one machine

The backend runs as four containers on a single Compute Engine VM. This is not the shape in
`infra/terraform/` — that is GKE Autopilot with Cloud SQL, which is right for something that
has to stay up and wrong for a week of judging at $215–250/month. Everything here costs
roughly $15 for that week.

What is genuinely lost by choosing it: Postgres is a container on a local disk, so there are
no managed backups and no point-in-time recovery. A lost disk is a lost database. The demo's
data is seeded and reproducible, which is the only reason that is acceptable.

## What exists

| | |
| --- | --- |
| Instance | `razorsharp-demo`, `asia-south1-a`, e2-standard-2 (2 vCPU / 8 GB), 50 GB pd-balanced |
| Project | `project-b9d1f592-6366-4856-884` |
| External IP | `34.93.90.117` (ephemeral, not reserved) |
| Tree | `/opt/razorsharp` |
| Secrets | `/opt/razorsharp/infra/gce/.env`, mode 600, never in git |
| Logs | `/var/log/razorsharp-deploy.log`, `/var/log/razorsharp-seed.log` |

Services, all healthy, all bound to loopback only — **nothing is reachable from the internet
yet, and no firewall rule opens anything**:

    api    127.0.0.1:8000   89 routes
    voice  127.0.0.1:8100   WebSocket gateway
    worker :8001 health only, no published port
    db     :5432 inside the compose network only

Seeded state: 5 orders, 3 refunds, 8 checkouts, 3 support cases, 6 payment attempts,
90 audit events.

## Running it again

Both scripts are idempotent. Run them detached: `gcloud compute ssh` drops the connection on
long jobs and takes the foreground process with it.

    gcloud compute ssh razorsharp-demo --zone asia-south1-a --command \
      'sudo bash -c "cd /opt/razorsharp && setsid nohup bash infra/gce/deploy.sh >> /var/log/razorsharp-deploy.log 2>&1 &"'

`deploy.sh` rebuilds changed images, re-applies migrations and grants, and restarts the
services. It does **not** re-seed: seeding resets the demo catalogue and would discard a
judge's session mid-demo. To seed deliberately, `infra/gce/seed.sh --reset`.

Stop the VM between demos. A stopped instance bills only the disk.

    gcloud compute instances stop razorsharp-demo --zone asia-south1-a
    gcloud compute instances start razorsharp-demo --zone asia-south1-a

Docker is `systemctl enable`d and every service is `restart: unless-stopped`, so a reboot
brings the stack back on its own.

## Two things that are still not done

**1. Vertex AI is refused.** The VM holds the `cloud-platform` scope but its service account
holds no IAM role, so voice and the agents answer 403. Proven from the machine rather than
assumed:

    Permission 'aiplatform.endpoints.predict' denied on resource
    .../publishers/google/models/gemini-3.8-flash

One command fixes it:

    gcloud projects add-iam-policy-binding project-b9d1f592-6366-4856-884 \
      --member="serviceAccount:864621313635-compute@developer.gserviceaccount.com" \
      --role=roles/aiplatform.user

**2. Nothing is public.** The plan is a Cloudflare Tunnel, which makes outbound connections
only and so needs no firewall rule, no reserved IP and no load balancer. It needs a login to
the Cloudflare account, which is where this hands over. The front end is a Cloudflare Worker
(`npm run build` produces one), not a container, so it does not belong on this VM either.

When the front end has a public origin, `VOICE_GATEWAY_ALLOWED_ORIGINS` must be set to it in
`/opt/razorsharp/infra/gce/.env` and the voice service restarted. It is unset today, which
means it defaults to `localhost:3000` and will refuse a browser arriving from anywhere else.

## The merchant workspace on a public host

`/api/merchant/...` mints its own MERCHANT session only when `NODE_ENV !== 'production'` and
both ends are localhost. On a deployed host that gate is closed by design, and the workspace
answers 401 until someone opens *Connect local demo merchant account* and pastes the scenario
key. That is correct for a privileged surface and it is a decision for the demo: either the
judges get the key, or the deployed build needs a gate of its own.
