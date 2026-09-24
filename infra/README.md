# infra

Status: not implemented — design only. See
`docs/architecture/12-local-development.md`.

- `docker-compose/` — local dev: Postgres, Redis, Prometheus, Loki,
  Grafana, and every `services/*` process, for fast inner-loop development.
- `kind/` — local Kubernetes cluster config for testing
  `remediation-executor`'s Kubernetes adapters and rehearsing production
  manifests.
- `k8s/` — base Kubernetes manifests (kustomize), with per-environment
  overlays including the `kind` overlay used by `infra/kind/`.
