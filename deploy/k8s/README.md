# Kubernetes reference manifests

Minimal, **unopinionated** manifests for running the platform on GKE, EKS, or any
Kubernetes cluster. They are a starting point — adapt image names, storage classes,
ingress annotations, and secrets to your organisation's standards.

Prefer the Helm chart at `deploy/helm/platform/` if you want values-based
configuration and optional Slack workers without maintaining copied YAML.

For architecture, managed-service mapping, and production checklists, see
[docs/cloud-deployment.md](../../docs/cloud-deployment.md).

## Contents

| File | Purpose |
|------|---------|
| `namespace.yaml` | `platform` namespace |
| `configmap.example.yaml` | Non-secret env (copy and edit) |
| `secret.example.yaml` | Secret placeholders (use External Secrets in prod) |
| `backend-deployment.yaml` | API Deployment + Service `backend:8000` (matches nginx proxy) |
| `frontend-deployment.yaml` | UI Deployment + Service (port 80) |
| `scheduler-deployment.yaml` | Scheduler worker (2 replicas with a Postgres leader lease; 1 on SQLite) |
| `ingress.example.yaml` | TLS ingress → frontend |

## Quick apply

1. Build and push images (see cloud-deployment doc):

   ```bash
   docker build -f backend/Dockerfile -t REGISTRY/platform-backend:TAG .
   docker build -f Dockerfile -t REGISTRY/platform-frontend:TAG .
   ```

2. Copy and edit examples:

   ```bash
   cp deploy/k8s/configmap.example.yaml deploy/k8s/configmap.yaml
   cp deploy/k8s/secret.example.yaml deploy/k8s/secret.yaml
   cp deploy/k8s/ingress.example.yaml deploy/k8s/ingress.yaml
   ```

3. Replace `REGISTRY/platform-*:TAG` in the Deployment manifests.

4. Apply:

   ```bash
   kubectl apply -f deploy/k8s/namespace.yaml
   kubectl apply -f deploy/k8s/configmap.yaml
   kubectl apply -f deploy/k8s/secret.yaml
   kubectl apply -f deploy/k8s/backend-deployment.yaml
   kubectl apply -f deploy/k8s/frontend-deployment.yaml
   kubectl apply -f deploy/k8s/scheduler-deployment.yaml
   kubectl apply -f deploy/k8s/ingress.yaml
   ```

## Notes

- **Postgres required** for more than one API replica (`DATABASE_URL` in Secret).
- **Mock auth** (`ENABLE_MOCK_AUTH=true`) is refused when `APP_ENV=production`.
- Point `MODEL_GATEWAY_URL` at your OpenAI-compatible gateway.
- Set `APP_BASE_URL` to the public HTTPS URL (OAuth callbacks).
- Workers (`slack_consumer_worker`, `warm_worker`) are not
  included — copy `scheduler-deployment.yaml` and change the command; see
  `docker-compose.workers.yml`.
- Sandbox execution on Kubernetes needs a sandbox provider wired to Jobs or
  gVisor/Kata — the default `container` sandbox expects a Docker socket.
