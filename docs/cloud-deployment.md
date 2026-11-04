# Cloud deployment (GKE, EKS, and similar)

This guide is for teams deploying the platform on **Google Kubernetes Engine (GKE)**, **Amazon Elastic Kubernetes Service (EKS)**, or any Kubernetes cluster with managed Postgres, object storage, and secrets.

The open-source repository ships **container images**, a **provider plugin model**, **reference compose files**, a **Helm chart** under `deploy/helm/platform/`, **minimal Kubernetes manifests** under `deploy/k8s/`, and **starter Terraform examples** under `deploy/terraform/` (AWS RDS + S3, GCP Cloud SQL + GCS). It does **not** ship cloud-specific provider implementations, EKS/GKE cluster modules, or production IAM — wire those to match your organisation's standards.

For first-time local setup, see [ADOPTER_GUIDE.md](ADOPTER_GUIDE.md). For Docker-only development, see [docker-setup.md](docker-setup.md).

---

## Contents

- [What you are deploying](#what-you-are-deploying)
- [Architecture](#architecture)
- [Build and publish images](#build-and-publish-images)
- [Managed services mapping](#managed-services-mapping)
- [Helm chart](#helm-chart)
- [Terraform examples](#terraform-examples)
- [Kubernetes workloads](#kubernetes-workloads)
- [Configuration](#configuration)
- [Sandbox on Kubernetes](#sandbox-on-kubernetes)
- [Slack at scale](#slack-at-scale)
- [GKE checklist](#gke-checklist)
- [EKS checklist](#eks-checklist)
- [Production verification](#production-verification)
- [What is not in this repository](#what-is-not-in-this-repository)

---

## What you are deploying

| Component | Image / process | Notes |
|-----------|-----------------|-------|
| **Frontend** | Root `Dockerfile` → nginx on port 80 | Serves static UI; proxies `/api` to backend |
| **Backend API** | `backend/Dockerfile` → uvicorn on port 8000 | Horizontally scalable when using Postgres + shared blob/secret stores |
| **Scheduler worker** | Same backend image, `python -m scheduler_worker` | **Exactly one replica** unless you add leader election |
| **Slack workers** | Same backend image | `slack_consumer_worker`, `warm_worker` — optional; need a real message queue |
| **Sandbox image** | `docker/sandbox/Dockerfile` | Model-generated Python runs here; no network at runtime |

---

## Architecture

```text
                         [Identity proxy]
           Cloudflare Access / Google IAP / ALB+OIDC / …
                                    │
                          [Ingress + TLS]
                                    │
              ┌─────────────────────┴─────────────────────┐
              │         Frontend (nginx) Deployment      │
              │              /api → backend Service        │
              └─────────────────────┬─────────────────────┘
                                    │
              ┌─────────────────────┴─────────────────────┐
              │         Backend API Deployment (N≥2)     │
              └──────┬──────────┬──────────┬────────────┘
                     │          │          │
            PostgreSQL    Object store   Secret backend
         (Cloud SQL/RDS)   (GCS/S3)    (Secret Manager / …)
                     │
              Redis (recommended) — skill cache invalidation, infra history, OAuth locks
                     │
         ┌───────────┴───────────┐
         │  Worker Deployments   │
         │  scheduler (×1)       │
         │  slack-* (optional)   │
         └───────────┬───────────┘
                     │
              Sandbox jobs/pods (gVisor/Kata or K8s Jobs)
                     │
              Model gateway (BYO — Vertex, Bedrock, LiteLLM, …)
```

**Request path:** browser → identity proxy → ingress → frontend → backend. Row-level tenancy uses the JWT `sub` claim. Background work (Slack, crons) runs in separate pods sharing the same database and provider configuration.

---

## Build and publish images

From the repository root:

```bash
# Backend API and workers
docker build -f backend/Dockerfile -t REGISTRY/platform-backend:TAG .

# Frontend (nginx + static assets)
docker build -f Dockerfile -t REGISTRY/platform-frontend:TAG .

# Sandbox (code execution)
docker build -f docker/sandbox/Dockerfile -t REGISTRY/platform-sandbox-python:TAG .
```

Push to your registry:

| Cloud | Registry |
|-------|----------|
| GCP | Artifact Registry (`REGION-docker.pkg.dev/PROJECT/REPO/...`) |
| AWS | Amazon ECR |

Set image pull secrets on your cluster if the registry is private.

---

## Managed services mapping

Replace local defaults with managed services before running more than one API replica.

| Concern | Local default | GCP | AWS | Env var |
|---------|---------------|-----|-----|---------|
| Database | SQLite (`hub.db`) | Cloud SQL (Postgres) | RDS Postgres | `DATABASE_URL` |
| Files / skills / blobs | `BLOB_STORE=local` | GCS bucket + plugin | S3 bucket + plugin | `BLOB_STORE=your.module:GcsBlobs` |
| Credentials | `SECRET_BACKEND=local` | Secret Manager + plugin | Secrets Manager + plugin | `SECRET_BACKEND=your.module:YourStore` |
| Background queue | `MESSAGE_QUEUE=inprocess` | Pub/Sub + plugin | SQS + plugin | `MESSAGE_QUEUE=your.module:PubSubQueue` |
| Cache (optional) | none | Memorystore Redis | ElastiCache Redis | `REDIS_HOST`, `REDIS_PORT` |
| Identity | mock auth | IAP, Cloudflare Access, Okta, … | Cognito, ALB OIDC, … | `OIDC_ISSUER`, `POLICY_AUD` |
| LLM | BYO gateway | Vertex / LiteLLM on GKE | Bedrock / LiteLLM on EKS | `MODEL_GATEWAY_URL` |

### Provider plugins

Built-in providers live in `backend/providers/`. For cloud deployments, implement your own classes and point env vars at them:

```bash
BLOB_STORE=acme.platform.gcp:GcsBlobStore
SECRET_BACKEND=acme.platform.gcp:GcpSecretBackend
MESSAGE_QUEUE=acme.platform.gcp:PubSubQueue
```

See `backend/providers/__init__.py` for interface contracts. **No GCP or AWS provider ships in the public tree** — `requirements-gcp.txt` lists SDK dependencies you may use in a private package.

### Database URL

```bash
DATABASE_URL=postgresql+asyncpg://USER:PASS@HOST:5432/platform
```

The backend normalises this to `postgresql+psycopg2://` for SQLAlchemy. Run migrations on deploy:

```bash
RUN_MIGRATIONS=true   # entrypoint runs `alembic upgrade head` once
```

Or run Alembic from a CI job before rolling out new API pods.

Each process (API replica, scheduler, worker) keeps its own connection pool:

```bash
DB_POOL_SIZE=10      # connections kept open
DB_MAX_OVERFLOW=20   # extra connections allowed under load
DB_POOL_TIMEOUT=30   # seconds to wait for a free connection
```

A process can open up to `DB_POOL_SIZE + DB_MAX_OVERFLOW` connections. Size Postgres `max_connections` (or your Cloud SQL / RDS tier) for that number times the process count across all Deployments, or put PgBouncer in front.

---

## Helm chart

The chart at `deploy/helm/platform/` deploys frontend, backend, scheduler, optional Slack workers, ConfigMap, Secret (or `existingSecret`), and Ingress.

```bash
helm upgrade --install platform ./deploy/helm/platform \
  -f my-values.yaml \
  --namespace platform --create-namespace
```

See [deploy/helm/platform/README.md](../deploy/helm/platform/README.md) for values and External Secrets wiring. Plain YAML equivalents remain in `deploy/k8s/`.

---

## Terraform examples

Starter data-plane Terraform lives in `deploy/terraform/`:

| Path | Provisions |
|------|------------|
| `deploy/terraform/aws/` | RDS PostgreSQL, S3 bucket, Secrets Manager `DATABASE_URL` |
| `deploy/terraform/gcp/` | Cloud SQL PostgreSQL, GCS bucket, Secret Manager `DATABASE_URL` |

These examples assume you already have a VPC and Kubernetes cluster. Outputs feed into Helm `secret.DATABASE_URL` and your private `BLOB_STORE` plugin. See [deploy/terraform/README.md](../deploy/terraform/README.md).

---

## Kubernetes workloads

Model each process as its own **Deployment** (or Job for one-off migrations). Start from the Helm chart, `deploy/k8s/`, or reference commands from `docker-compose.workers.yml`:

| Workload | Command | Replicas |
|----------|---------|----------|
| Frontend | nginx (image default) | 2+ |
| Backend API | `uvicorn main:app --host 0.0.0.0 --port 8000` | 2+ |
| Scheduler | `python -m scheduler_worker` | **1** |
| Slack consumer | `python -m slack_consumer_worker` | 1+ (if using Slack + queue) |
| Warm worker | `python -m warm_worker` | optional |

### Shared configuration

- Mount the **same ConfigMap / Secret** (or External Secrets) on API and worker pods: `DATABASE_URL`, `MODEL_GATEWAY_*`, `SECRET_ENCRYPTION_KEY`, provider overrides, `APP_BASE_URL`, OIDC settings, etc.
- **Skills data:** the backend image seeds `/src/data` on first run. For multiple API replicas, use a **ReadWriteMany PVC** or store published skills in your blob provider so all pods see the same catalogue.
- **Generated apps / blobs:** use a shared `BLOB_ROOT` on PVC only for single-cluster experiments; prefer `BLOB_STORE` pointing at object storage for production.

### Networking

- **Service:** `backend` ClusterIP on port 8000; frontend proxies `/api` to it.
- **Ingress:** TLS termination at the edge; set `APP_BASE_URL` to the public HTTPS URL (required for Slack/Google OAuth callbacks).
- **`/internal/*`:** do **not** expose on public ingress. The bundled nginx config returns 403 for `/internal/` on the UI port. Workers and relays reach the backend on the cluster network. Set `INTERNAL_SIGNING_SECRET` and `PLATFORM_INTERNAL_BASE_URL` (e.g. `http://backend:8000`).

### Health checks

```http
GET /api/health
```

Use for Kubernetes `livenessProbe` and `readinessProbe` on backend pods.

---

## Configuration

Minimum production env (in addition to wizard-generated `platform.env` values):

```bash
APP_ENV=production
ENABLE_MOCK_AUTH=false

OIDC_ISSUER=https://your-issuer.example.com
POLICY_AUD=your-audience

DATABASE_URL=postgresql+asyncpg://...

SECRET_ENCRYPTION_KEY=<base64-32-bytes>   # back up; losing it loses all stored credentials
RESOURCE_PREFIX=your-org

MODEL_GATEWAY_URL=https://your-gateway.example.com
MODEL_GATEWAY_API_KEY=...

APP_BASE_URL=https://platform.yourcompany.com
PLATFORM_INTERNAL_BASE_URL=http://backend:8000
INTERNAL_SIGNING_SECRET=<random-hs256-secret>

# Multi-replica (implement plugins first)
BLOB_STORE=your.module:YourBlobStore
SECRET_BACKEND=your.module:YourSecretBackend
MESSAGE_QUEUE=your.module:YourQueue
REDIS_HOST=redis.example.internal

# Sandbox
SANDBOX_PYTHON_IMAGE=REGISTRY/platform-sandbox-python:TAG
SANDBOX_PYTHON_PACKAGES=requests, httpx, pandas, ...
```

Complete the setup wizard once (`/admin/setup`) or run `npm run setup` before first production boot so branding, gateway, and encryption key are set.

---

## Sandbox on Kubernetes

The default `SANDBOX_PROVIDER=container` talks to a container engine (Docker socket). **Avoid mounting the Docker socket on API pods in production** when possible.

| Approach | When to use |
|----------|-------------|
| **K8s Job sandbox** | Preferred on GKE/EKS — implement `SandboxProvider` using `k8s-agent-sandbox` (`pip install -r requirements-k8s.txt`) |
| **gVisor / Kata** | Stronger isolation with `SANDBOX_RUNTIME=runsc` on nodes that support it |
| **Docker socket** | Acceptable only on dedicated sandbox nodes with tight RBAC |

Build and push `platform-sandbox-python` to your registry. Whatever packages the model may import must be **in the image** — runs have no network to `pip install` at runtime. Keep `SANDBOX_PYTHON_PACKAGES` in sync with the image contents.

---

## Slack at scale

| Topology | Setup |
|----------|--------|
| Single API replica | Point Slack Request URL at `{APP_BASE_URL}/api/slack/events` |
| Multiple API replicas | Run your own **events relay**; set `SLACK_EVENTS_GATEWAY_URL`. See [slack-dmz-relay.md](slack-dmz-relay.md) for the implementation spec. |
| Slack workers | Set `MESSAGE_QUEUE` to a durable queue; run `slack_consumer_worker` and optionally `warm_worker` |

---

## GKE checklist

### Cluster and networking

- [ ] GKE cluster (Autopilot or Standard) with Workload Identity or node service account
- [ ] Artifact Registry repository; images pushed and pullable from cluster
- [ ] Ingress (GCE Ingress or Gateway API) + managed certificate or cert-manager
- [ ] Identity: Cloudflare Access, Google IAP, or OIDC proxy in front of ingress

### Data plane

- [ ] Cloud SQL for PostgreSQL; private IP or Auth Proxy sidecar
- [ ] `DATABASE_URL` in Secret; `RUN_MIGRATIONS=true` on first deploy or CI migration job
- [ ] GCS bucket + `BLOB_STORE` plugin (or RWX PVC for early pilots only)
- [ ] Secret Manager + `SECRET_BACKEND` plugin for agent/Slack tokens
- [ ] Memorystore Redis for `REDIS_HOST` (recommended with 2+ API replicas)
- [ ] Pub/Sub topic + `MESSAGE_QUEUE` plugin if using Slack workers

### Workloads

- [ ] Deployments: `frontend`, `backend` (≥2 replicas), `scheduler-worker` (1 replica)
- [ ] Optional: `slack-consumer-worker`, `warm-worker`
- [ ] `backend` Service; frontend nginx `proxy_pass` to `http://backend:8000`
- [ ] Probes on `/api/health`
- [ ] PVC or blob store for `/src/data` skills volume if multiple API pods publish skills

### Security

- [ ] `ENABLE_MOCK_AUTH=false`, `APP_ENV=production`
- [ ] `SECRET_ENCRYPTION_KEY` in Secret Manager / K8s Secret (not in git)
- [ ] `/internal/*` not on public ingress; `INTERNAL_SIGNING_SECRET` set
- [ ] Sandbox: gVisor node pool or K8s sandbox provider — no Docker socket on API pods
- [ ] `RESOURCE_PREFIX` unique per environment if sharing GCP project

### Optional

- [ ] `pip install -r requirements-k8s.txt` in backend image for Admin → Infrastructure metrics tab
- [ ] `SANDBOX_NAMESPACE=agent-sandbox` if using in-cluster sandbox jobs

---

## EKS checklist

### Cluster and networking

- [ ] EKS cluster with IRSA (IAM Roles for Service Accounts)
- [ ] Amazon ECR repositories; images pushed and pullable
- [ ] AWS Load Balancer Controller or Ingress-NGINX + ACM certificate
- [ ] Identity: ALB OIDC, Cognito, Cloudflare Access, or similar

### Data plane

- [ ] RDS PostgreSQL (Multi-AZ for production)
- [ ] `DATABASE_URL` in Secrets Manager or External Secrets
- [ ] S3 bucket + `BLOB_STORE` plugin
- [ ] Secrets Manager + `SECRET_BACKEND` plugin
- [ ] ElastiCache Redis for `REDIS_HOST`
- [ ] SQS (+ SNS if needed) + `MESSAGE_QUEUE` plugin for Slack workers

### Workloads

- [ ] Same Deployment layout as GKE (frontend, backend, scheduler, optional workers)
- [ ] IRSA policies granting S3, Secrets Manager, SQS access to pod roles
- [ ] Probes on `/api/health`

### Security

- [ ] Same production auth and internal-route rules as GKE checklist
- [ ] Sandbox on dedicated node group or Fargate profiles with restricted IAM
- [ ] No Docker socket on API pods unless explicitly accepted risk

### Optional

- [ ] `requirements-k8s.txt` for infra monitoring tab
- [ ] VPC endpoints for S3/RDS/Secrets Manager if traffic must stay private

---

## Production verification

After deploy:

1. `GET /api/health` returns 200 from inside and outside the cluster (via ingress).
2. `POST /api/admin/setup/verify` (as admin) shows green checks for gateway, encryption, auth, sandbox, blob.
3. Sign in through your real IdP — confirm admin vs member roles from group claims.
4. Workspace chat completes against your model gateway.
5. If using Slack: register a test agent, send a mention, confirm event reaches backend (check worker logs if using a queue).
6. If using Lab / `run_python`: trigger a skill that executes code; confirm sandbox pod/job completes.

See [ADOPTER_GUIDE.md § Production checklist](ADOPTER_GUIDE.md#10-production-checklist) and [SECURITY.md](../SECURITY.md).

---

## What is not in this repository

| Missing artifact | What adopters do instead |
|------------------|--------------------------|
| EKS / GKE cluster + IAM | Use your platform team's cluster modules; wire IRSA / Workload Identity for S3/GCS access |
| Cloud provider Python packages | Implement `BLOB_STORE`, `SECRET_BACKEND`, `MESSAGE_QUEUE` plugins privately — see `backend/providers_reference/README.md` |
| Kustomize overlays | Start from `deploy/helm/platform/` or `deploy/k8s/` |
| Slack multi-replica relay | Build and operate your own gateway; set `SLACK_EVENTS_GATEWAY_URL` — [slack-dmz-relay.md](slack-dmz-relay.md) |
| Managed model gateway | Bring LiteLLM, Vertex, Bedrock, OpenRouter, or self-hosted vLLM |

The **provider interfaces** in `backend/providers/` are the stable contract. The **Docker images and env vars** are the portable deployment unit. Everything else is intentionally left to the adopter's cloud standards.

---

## Related docs

- [ADOPTER_GUIDE.md](ADOPTER_GUIDE.md) — setup wizard, OIDC, directory, Slack, production checklist
- [docker-setup.md](docker-setup.md) — mock auth, compose, sandbox on a single host
- [README.md § Operating it](../README.md#operating-it) — security notes before real users
- `deploy/helm/platform/` — Helm chart
- `deploy/terraform/` — starter AWS/GCP data-plane examples
- `deploy/k8s/` — reference Kubernetes manifests
- `docker-compose.workers.yml` — reference worker commands and env
- `backend/providers_reference/README.md` — cloud provider sketch
- `backend/.env.example` — full configuration surface
