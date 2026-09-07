# Kubernetes manifests (Kustomize)

```
base/
  platform/    namespace (PSA restricted), ServiceAccounts, platform-config ConfigMap,
               default-deny + DNS + metadata-server NetworkPolicies
  workloads/   commerce-api, action-executor, buyer-web, merchant-console (Deployments, Services,
               BackendConfigs), Ingress + FrontendConfig + ManagedCertificate, PDBs,
               per-workload NetworkPolicies and FQDNNetworkPolicies
  jobs/db-migrate/  `alembic upgrade head` Job (run before every rollout)
overlays/
  dev/, demo/  platform/ (Workload Identity annotations, ConfigMap values,
               SecretProviderClasses), images/ (Component with the registry + tag),
               kustomization.yaml (host, static IP, TLS policy), db-migrate/
```

Render and inspect before applying:

```sh
kubectl kustomize infra/kubernetes/overlays/demo | less
kubectl kustomize infra/kubernetes/overlays/demo/db-migrate | less
```

## Filling in an overlay

Placeholders: `PROJECT_ID`, `REDIS_HOST`, the host name (`commerce.example.com` in demo,
`dev.commerce.example.com` in dev) and the instance connection name suffix
(`commerce-<env>-pg-01`, matches `db_instance_suffix` in Terraform).

```sh
ENV=demo PROJECT_ID=my-project HOST=shop.example.org REDIS_HOST=$(cd infra/terraform && terraform output -raw redis_host)
grep -rl 'PROJECT_ID\|REDIS_HOST\|commerce.example.com' infra/kubernetes/overlays/$ENV \
  | xargs sed -i '' -e "s/PROJECT_ID/$PROJECT_ID/g" -e "s/REDIS_HOST/$REDIS_HOST/g" -e "s/commerce.example.com/$HOST/g"
kubectl kustomize infra/kubernetes/overlays/$ENV | grep -c 'SET-BY-OVERLAY\|PROJECT_ID'   # must print 0
```

(GNU sed: drop the `''` after `-i`.)

## Design notes

- One replica of the API and of the worker (ADR 0003 D14), `Recreate` strategy so two
  processes never overlap. `WEB_CONCURRENCY=1`.
- Database identities: each Pod runs Cloud SQL Auth Proxy native sidecars, one per
  PostgreSQL role, each impersonating a dedicated service account that is an IAM database
  user. `127.0.0.1:5432` is the process's primary role (app / worker), `127.0.0.1:5433`
  is `commerce_kernel`. The role grant set is the physical boundary (ADR D1).
- Secrets are mounted by the GKE Secret Manager add-on as files named after environment
  variables; `infra/docker/entrypoint.py` exports them. Nothing secret is in a ConfigMap
  or in git.
- NetworkPolicy: default deny both directions; DNS and the metadata server for every Pod;
  API and web accept only load-balancer traffic (and web -> API); egress to the private
  services range for Cloud SQL/Memorystore; hostname allowlists (`api.razorpay.com`
  for the worker, `*.googleapis.com` for proxies/Gemini) are FQDNNetworkPolicies, which
  need `enable_fqdn_network_policy = true` on the cluster (second Terraform apply).
- BackendConfig timeouts are 3600 s on both backends: the SSE stream on
  `/v1/checkouts/{id}/events` traverses the Next.js route handler as well as the API.
- Probes: `/healthz` readiness/liveness for the API (liveness slow, for deadlock recovery
  only), `/` for the web front end, `/healthz` on port 8001 liveness for the worker (a
  contract for `action_executor.main`; opt-out patch in the overlay).
- Autopilot: requests equal limits, CPU:memory within 1:1–1:6.5, seccomp RuntimeDefault,
  all capabilities dropped, read-only root filesystem with an emptyDir at `/tmp`.
