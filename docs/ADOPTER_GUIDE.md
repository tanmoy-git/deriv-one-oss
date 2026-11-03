# Adopter guide

This document is for teams self-hosting the platform (default branding: **Platform**). It covers the minimum configuration to run without vendor-specific internal services.

## Environment files (one story)

| File | Purpose |
|------|---------|
| `backend/platform.env` | **Primary config** — written by `npm run setup` or the web wizard. Holds gateway keys, branding, OIDC/mock auth, Slack auto-create tokens, and encryption key. Gitignored. |
| `backend/.env` | **Extras** — copy from `backend/.env.example` for values the wizard does not set: `DATABASE_URL`, sandbox image names, optional provider overrides, `APP_BASE_URL` for tunnels. Loaded before `platform.env`; wizard values win on conflict. |
| `backend/platform.env.example` | Safe template showing the shape of `platform.env` — no real secrets. |

After setup, restart the backend and use **Verify backend** on the setup completion screen (or `POST /api/admin/setup/verify`) to see green/red checks for gateway, auth, sandbox, blob storage, and optional Slack auto-create.

## Terminal setup (recommended for local dev)

Run **before** starting the backend so `platform.env` is ready on first boot:

```bash
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
npm run setup          # full interactive wizard
npm run setup -- --quick --test-gateway   # personal mode, fewer prompts
```

The script writes `backend/platform.env` using the same pipeline as the web wizard,
then offers to start both servers (or pass `--start` to skip the prompt).

To start the stack any time without re-running setup:

```bash
npm run dev:stack
```

## Web setup wizard

After signing in as an admin, open **Settings → Platform setup** or go to `/admin/setup`.
Use this to **reconfigure** an already-running instance (requires a backend restart afterward).

The wizard walks through:

1. **Personal vs team** deployment mode
2. **Branding** — name, logo, colors, public URL
3. **Models** — gateway URL, API key, encryption key (with live connection test)
4. **Identity** — mock auth (personal) or OIDC issuer + audience (team)
5. **Directory** — JWT claims or uploaded YAML file
6. **Slack** — skip, manual apps, or optional auto-create tokens
7. **Review** — writes `platform.env` and marks setup complete

Restart the backend after finishing so model gateway, identity, and Slack env vars load.

For existing deployments that are already configured, set `PLATFORM_SETUP_COMPLETE=1` or complete the wizard once to dismiss the admin redirect.

## Configuration map

| Area | Setup wizard | Must edit env | Optional |
|------|--------------|---------------|----------|
| Branding | `APP_NAME`, logo, colors | — | `ORG_NOUN` |
| Model gateway | URL, encryption key (API key optional for local gateways) | Bring your own OpenAI-compatible gateway | `MODEL`, catalog TTL |
| Auth | Mock (personal) or OIDC issuer + audience | Team mode: real OIDC proxy in front | `ADMIN_GROUPS`, JWKS override |
| Directory | Claims or YAML upload | File path if using `DIRECTORY_PROVIDER=file` | Custom `DirectoryProvider` plugin |
| Slack | Skip / manual / auto-create tokens | `SLACK_CONFIG_COLLABORATOR` for auto-create | Per-user OAuth client ids in `.env` |
| Persistence | SQLite + local blob implied | `DATABASE_URL` for multi-replica | `BLOB_STORE`, `SECRET_BACKEND` plugins |
| Sandbox | Defaults to container provider | Docker socket / images for Lab + `run_python` | `SANDBOX_RUNTIME=gVisor`, K8s provider |
| Public URL | `APP_BASE_URL` in wizard | Tunnel URL for Slack/Google callbacks | `SLACK_EVENTS_GATEWAY_URL` |

**Personal/local defaults:** mock auth on, SQLite database, local encrypted secrets, local blob store, claims-based directory. **Team mode** forces OIDC and refuses mock auth when `APP_ENV=production`.

## 1. Branding

Set in `platform.env` (wizard) or `backend/.env` (exposed via `GET /api/platform/config`):

| Variable | Purpose | Default |
|----------|---------|---------|
| `APP_NAME` | Product name in prompts and UI | `Platform` |
| `APP_SHORT_NAME` | Sidebar wordmark | same as `APP_NAME` |
| `APP_LOGO_URL` | Logo path or URL | `/app-logo-icon.svg` |
| `ORG_NOUN` | Wording in system prompts (`employee`, `teammate`, …) | `employee` |
| `BRAND_COLOR` / `BRAND_COLOR_HOVER` / `BRAND_COLOR_SOFT` | Theme accents | coral theme |

Replace `public/app-logo-icon.svg` or point `APP_LOGO_URL` at your own asset.

## 2. Identity (OIDC)

1. Put an OIDC-capable proxy or API gateway in front of the app that forwards a signed JWT.
2. Configure:
   - `OIDC_ISSUER` — JWT `iss` claim (legacy alias: `TEAM_DOMAIN`)
   - `POLICY_AUD` — required audience
   - `OIDC_JWKS_URL` — optional; if unset, discovery is tried at `{issuer}/.well-known/openid-configuration`
3. Map IdP groups to app roles with `ADMIN_GROUPS`, `MEMBER_GROUPS`, `GROUPS_JWT_CLAIM_KEYS`.
4. Set `ENABLE_MOCK_AUTH=false` before any shared/production deployment. The server **refuses to start** with mock auth when `APP_ENV=production`.

Supported JWT carriers: `Cf-Access-Jwt-Assertion` (Cloudflare Access) or `Authorization: Bearer`.

## 3. Org directory

| You have | Set |
|----------|-----|
| Nothing yet | `DIRECTORY_PROVIDER=claims` — approvals route to admins |
| HRIS / CSV export | `DIRECTORY_PROVIDER=file` + `data/directory.example.yaml` |
| Workday / Okta / LDAP / custom HRIS | Implement `DirectoryProvider` in a **private** plugin package |

No vendor-specific directory connector ships in this repository.

## 4. Model gateway

Point `MODEL_GATEWAY_URL` at any OpenAI-compatible endpoint (LiteLLM, OpenRouter, Azure OpenAI, Anthropic via gateway, etc.). The model picker reflects whatever that gateway advertises.

**Local models (LM Studio, Ollama):** use a loopback URL such as `http://127.0.0.1:1234` and leave `MODEL_GATEWAY_API_KEY` empty. The setup wizard probes `/v1/models` without auth and stores a harmless local placeholder when the gateway is on localhost. Cloud gateways still require a real API key.

**Model allowlists:** gateways vary — LM Studio and Ollama return whatever ids they load, not a LiteLLM-style catalog. Each user can choose models under **Settings → Models**. Admins can optionally cap the deployment with `WORKSPACE_ENABLED_MODELS` (comma-separated ids in `platform.env`). Embedding models are hidden from pickers by default.

## 5. Slack (optional)

**Manual path (no org-admin tokens):** create Slack apps yourself, paste bot token + signing secret. No collaborator step.

**Auto-create / manifest sync (optional):**

- `SLACK_APP_CONFIGURATION_ACCESS_TOKEN` + `SLACK_APP_CONFIGURATION_REFRESH_TOKEN` — powerful org-admin API; treat as highly sensitive.
- `SLACK_CONFIG_COLLABORATOR` — Slack username that must be added as collaborator on self-created apps.
- `SLACK_MANIFEST_IP_ALLOWLIST` — comma-separated CIDRs appended when an app already restricts API token usage by IP.

## 6. Secrets backend

Default: local encrypted files (`SECRET_BACKEND=local`). For shared/multi-replica deployments, implement or plug in your vault via `SECRET_BACKEND=your.module:YourStore`. User and agent secret names are prefixed with `RESOURCE_PREFIX`.

## 7. Sandbox and blob storage

| Variable | Default (local) | When to change |
|----------|-----------------|----------------|
| `SANDBOX_PROVIDER` | `container` | Use `subprocess` only on trusted single-user machines |
| `BLOB_STORE` | `local` | Point at your object store plugin for multi-replica |
| `SANDBOX_PYTHON_IMAGE` | `platform/sandbox-python:latest` | Build/push your own sandbox image in air-gapped environments |

Lab builds and skill `run_python` need a working sandbox provider. The setup health check reports the configured provider but does not start a container — verify Docker (or your runtime) separately.

## 8. Background workers

The main `docker compose up` stack runs the API and UI only. Optional workers live in `docker-compose.workers.yml`:

```bash
docker compose -f docker-compose.yml -f docker-compose.workers.yml up -d
```

| Service | Purpose |
|---------|---------|
| `scheduler-worker` | Automation crons and memory mining. One replica is enough. On PostgreSQL you can run two: they elect a leader and the standby takes over within `SCHEDULER_LEASE_CHECK_S` seconds. On SQLite, **run exactly one**. |
| `slack-consumer-worker` | **Default** Slack worker — pull events from a message queue (profile: `slack`) |
| `warm-worker` | Optional high-throughput Slack consumer (profile: `slack`) — do not run alongside `slack-consumer-worker` on the same queue |

For Kubernetes, run each worker as its own Deployment with the same env as the API pods.

## 9. Internal routes and multi-replica Slack

- `/internal/*` is **not** proxied through the public nginx port (`:8080`). Workers and relays must call the backend directly on the private network.
- Set `INTERNAL_SIGNING_SECRET` and `PLATFORM_INTERNAL_BASE_URL` (legacy alias: `DERIV_ONE_BASE_URL`) so agent-to-agent calls and DMZ relays can authenticate.
- `SLACK_EVENTS_GATEWAY_URL` is for your own relay when running multiple API replicas — no relay ships in this repository. Implementation spec: [slack-dmz-relay.md](slack-dmz-relay.md).

## 10. Operational runbooks

Token rotation and compliance exports are **not** shipped as scripts in this repository. See:

- [runbooks/slack-token-rotation.md](runbooks/slack-token-rotation.md)
- [runbooks/agent-memory-export.md](runbooks/agent-memory-export.md)

Implement these in your private ops environment against your `SECRET_BACKEND` and `BLOB_STORE`.

## 11. Cloud deployment (GKE, EKS)

For Kubernetes on GCP, AWS, or similar: architecture, managed-service mapping, workload layout, and step-by-step checklists are in **[cloud-deployment.md](cloud-deployment.md)**. The repository ships a Helm chart (`deploy/helm/platform/`), plain Kubernetes manifests (`deploy/k8s/`), and starter Terraform for RDS/Cloud SQL + object storage (`deploy/terraform/`). Cloud provider plugins and cluster/IAM modules are still yours to implement.

## 12. Production checklist

- [ ] `ENABLE_MOCK_AUTH=false`
- [ ] `APP_ENV=production`
- [ ] `OIDC_ISSUER` + `POLICY_AUD` configured; JWKS resolves
- [ ] `RESOURCE_PREFIX` unique per deployment if sharing a vault
- [ ] `SANDBOX_PROVIDER` reviewed (container vs subprocess vs gVisor/Kata)
- [ ] `/internal/*` reachable only on private network; set `INTERNAL_SIGNING_SECRET`
- [ ] Redis for multi-replica skill invalidation / infra history (optional single-node)
- [ ] `pip install -r requirements-k8s.txt` only if you need K8s sandbox or infra metrics
- [ ] `LOG_FORMAT=json` for your log shipper; `METRICS_ENABLED=true` with an in-cluster Prometheus scrape (optional `METRICS_TOKEN`). See [cloud-deployment.md § Logs and metrics](cloud-deployment.md#logs-and-metrics)

See [SECURITY.md](../SECURITY.md) for reporting vulnerabilities.
