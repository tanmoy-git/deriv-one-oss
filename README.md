# Deriv One

**Deriv One** is an open-source, self-hostable platform for governed AI at work. Your
team gets a shared workspace — chat, skills, Slack agents, sandboxed Python, scheduled
automations, and a small app builder — while credentials and models stay on
**your** infrastructure and **your** model gateway.

[Repository](https://github.com/deriv-com/deriv-one-oss) ·
[MIT License](https://github.com/deriv-com/deriv-one-oss/blob/master/LICENSE) ·
[Security policy](https://github.com/deriv-com/deriv-one-oss/blob/master/SECURITY.md)

![Deriv One workspace, skills, and Lab](deriv-one-demo-no-sidebar.gif)

---

## Contents

- [What you get](#what-you-get)
- [What you need](#what-you-need)
- [Quick start](#quick-start)
- [Documentation](#documentation)
- [Why it is shaped this way](#why-it-is-shaped-this-way)
- [The components](#the-components)
- [The provider layer](#the-provider-layer)
- [Configuration](#configuration)
- [Request path](#request-path)
- [Repository layout](#repository-layout)
- [Operating it](#operating-it)
- [Callback and request URLs](#callback-and-request-urls)
- [Status](#status)
- [Licence](#licence)

---

## What you get

| Capability | In plain terms |
|------------|----------------|
| **Workspace chat** | Multi-tab chat with skills, context limits, and model routing tuned per message. |
| **Skills** | Versioned, reviewable instruction packs (plus optional Python artifacts) with per-person access control. |
| **Agents** | Register Slack bots with their own app credentials, memory, knowledge bases, and schedules. |
| **Sandbox** | Model-generated Python runs in disposable containers — not on your laptop with full secrets. |
| **Lab** | Describe an internal app in chat, preview it, and host it on your instance. |
| **Automations & flows** | Cron jobs and user-owned skill chains for repeat work. |

Operators configure identity (OIDC or mock auth for local dev), attach an OpenAI-compatible **model gateway**, and optionally wire Slack and Google OAuth. Nothing in the repo assumes a specific cloud vendor.

---

## What you need

| Requirement | Notes |
|-------------|--------|
| **Model gateway** | Any OpenAI-compatible HTTP API (LiteLLM, vLLM, Ollama with a proxy, a cloud gateway you control, etc.). |
| **Docker** (recommended) | For the default stack and for sandbox execution. Local dev without Docker is possible with reduced isolation — see [Quick start](#quick-start). |
| **Node.js 20+** | UI and `npm run setup` / `npm run dev`. |
| **Python 3.12+** | API backend. |
| **Identity (production)** | Put a real OIDC/JWT proxy in front of the app and turn off mock auth. |

---

## Why it is shaped this way

Four decisions explain most of the design. Everything else follows from them.

**1. Everything outside the process is a provider.**
Storing files, storing credentials, running untrusted code, knowing who works
here, searching documents, moving work between processes, raising alerts — each
is an interface in `backend/providers/` with a self-contained reference
implementation. No module in that package imports a hosting vendor's SDK, which
is why the project runs on a laptop with no external accounts. A deployment that
wants managed infrastructure supplies a class and points an environment variable
at it; the platform never learns that class exists.

**2. Model-generated code is untrusted code.**
Not because the model is hostile, but because the instructions reaching it may
be. Code runs in a disposable container with no credentials, no network by
default, dropped capabilities and a read-only root, and the container is
destroyed afterwards. Isolation strength varies by deployment, so it is part of
the interface: every sandbox provider carries an `isolation` string stating what
it actually guarantees, and that string is shown to operators. Nobody should
have to read source to find out how well isolated their execution is.

**3. The database holds references, never secrets.**
Writing a secret returns an opaque reference. That reference is what gets stored
next to an agent, skill or user. Resolving it to a value happens only in the
process about to use it, and the value is never logged, returned by an API, or
written to the database. This holds regardless of backend — swapping the storage
cannot weaken it, because the contract lives above the storage.

**4. Each agent invocation is its own process.**
An agent request runs in a dedicated subprocess loading only that agent's
credentials. Two agents' secrets never occupy one address space, so a prompt
injection against one agent cannot read another's tokens out of memory. It costs
a process spawn per invocation. That is the price of the guarantee.

---

## Quick start

These steps get Deriv One running on your machine for exploration. For production,
follow [Operating it](#operating-it) and [docs/ADOPTER_GUIDE.md](docs/ADOPTER_GUIDE.md).

### 1. Clone and install

```bash
git clone https://github.com/deriv-com/deriv-one-oss.git
cd deriv-one-oss
cp backend/.env.example backend/.env
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd .. && npm install
```

### 2. Run the setup wizard (recommended)

The wizard writes `backend/platform.env` (gateway, encryption key, auth mode) and can
probe your model gateway:

```bash
npm run setup -- --quick --test-gateway
```

When it finishes, start the stack (or accept the prompt to start automatically):

```bash
npm run dev:stack
```

- **UI (dev):** <http://localhost:3000> (proxies API to port 8000)
- **Sign-in:** with mock auth enabled, you are signed in as the admin user defined in env (see `backend/.env.example`).

On first login, admins can open **Settings → Platform setup** (`/admin/setup`) to adjust branding, models, identity, directory, and Slack.

### 3. Or use Docker Compose

Set three values in `backend/.env` (the wizard can do this for you instead):

| Variable | Why |
|---|---|
| `MODEL_GATEWAY_URL` | Any OpenAI-compatible endpoint. The model list comes from whatever this reports. |
| `MODEL_GATEWAY_API_KEY` | Its key. |
| `SECRET_ENCRYPTION_KEY` | 32 bytes of base64 (below). Losing it means losing every stored credential. |

```bash
python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
docker compose up --build
```

If your model gateway runs on your own machine, note that `localhost` inside the
backend container is that container, not your machine: use
`http://host.docker.internal:4000` (Docker Desktop) or `http://172.17.0.1:4000`
(Linux). That is what `backend/.env.example` ships.

The UI is on <http://localhost:8080>.

That stack runs with mock authentication and local providers — files on disk, an
in-process queue, SQLite. **`ENABLE_MOCK_AUTH=true` is the shipped default so
that the stack is usable before you have an identity provider, and it is the
entire authentication check: anyone who can reach the port is `MOCK_USER_EMAIL`,
an admin.** It is a development stack. See [Operating it](#operating-it) before
putting it in front of anyone.

The first build also builds `docker/sandbox/Dockerfile` and tags it
`platform/sandbox-python:latest` — the image model-generated Python runs in. A
run has no network, so it cannot install anything; whatever code needs must be
in that image, and `SANDBOX_PYTHON_PACKAGES` must list it, because that list is
what the model is told it can import. Change one, change the other.

### Without containers

```bash
cd backend && python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn main:app --reload --port 8000

npm install && npm run dev        # in a second shell
```

The dev UI is on <http://localhost:3000> and proxies `/api` to port 8000
(override the backend port with `DEV_API_PORT`).

Two of the shipped defaults assume containers, so change them for this path:
`MODEL_GATEWAY_URL` back to `http://localhost:4000` if your gateway is local,
and `SANDBOX_WORKSPACE_ROOT` unset (it already is). `ENABLE_MOCK_AUTH=true` is
already the default.

For code execution without a container engine, set `SANDBOX_PROVIDER=subprocess`
and `ALLOW_UNSANDBOXED_EXECUTION=1` — and understand that this runs
model-generated code directly on your machine, with your files and your
credentials, and no isolation of any kind.

---

## Documentation

| Guide | Use when |
|-------|----------|
| [docs/ADOPTER_GUIDE.md](docs/ADOPTER_GUIDE.md) | Self-hosting checklist — auth, directory, Slack, workers, env files. |
| [docs/docker-setup.md](docs/docker-setup.md) | Docker-focused setup and troubleshooting. |
| [docs/cloud-deployment.md](docs/cloud-deployment.md) | Kubernetes / Helm / cloud data plane. |
| [docs/slack-app-setup.md](docs/slack-app-setup.md) | Register Slack apps manually. |
| [docs/slack-dmz-relay.md](docs/slack-dmz-relay.md) | Multi-replica Slack event relay. |
| [SECURITY.md](SECURITY.md) | Reporting vulnerabilities (private disclosure). |

---

## The components

### Skills — the core primitive

A skill is a markdown file with frontmatter: instructions describing what it is
for, when to use it, and — just as importantly — when not to. Skills are
versioned, reviewed before publication, and visible per person.

| Concern | Where |
|---|---|
| Access manifest and per-person resolution | `skills_acl.py`, `src/data/skills-acl.yaml` |
| Publication pipeline with AI security review | `api_skill_publish.py`, `api_skill_create.py` |
| Version history and markdown diff | `skill_version_history.py`, `SkillVersionHistoryTab.tsx` |
| Atomic edits (optimistic `base_hash` + per-slug lock) | `skill_edit_commit.py` |
| Durable mirror to the blob store | `skills_store.py`, `skills_invalidate.py` |
| Intent routing | `skill_router.py`, `skill_index.py` |
| Co-ownership | `skill_collaborators.py` |

**Routing** classifies an incoming request against the catalogue the user is
permitted to see, then verifies the candidate against its full instructions
before suggesting it — a description match alone is too weak a signal to act on.

**Artifacts** are Python a skill may call. A manifest declares the hosts it
reaches and the secrets it reads; a deterministic static gate (`artifact_gate.py`)
reviews it before approval; and a fingerprint of the approved source
(`code_fingerprint.py`) is retained so approved code cannot later drift. Secrets
and PII are stripped before any generated code is persisted (`redaction.py`).

### Agents

Name, persona, model, attached skills, access grants. Registered through
`api_agents.py`, stored by `agents.py`, and executed by `agent_worker.py` — one
subprocess per invocation, loading only that agent's credentials.

- **Model resolution** — `agent_llm.py`, `model_catalog.py`. An allowlist of
  gateway aliases; raw provider slugs are rejected. Transport is chosen per
  model, because only Anthropic-transport calls can carry prompt-cache markers.
- **Memory** — `memory_store/`. Consolidation, retention policy, an append-only
  episode archive that records *how* an agent came to believe something, and
  mining jobs (`agent_memory_jobs.py`).
- **Knowledge bases** — a private document set per agent with a search tool.
  Indexing is asynchronous by contract: `kb_ingest_worker.py` leases work so
  replicas do not collide, and per-document state drives the UI.
- **Scheduling** — `agent_scheduled_tasks.py`.
- **Roles and collaborators** — `api_agent_roles.py`.

### Slack

The most involved integration. Each agent is its own Slack app with its own
signing secret.

- **Inbound** (`api_slack_events.py`) — per-agent HMAC verification, a
  five-minute replay window, event-id de-duplication with a *leased* claim so a
  crashed worker's events are reclaimed rather than lost.
- **Reply gate** (`slack_reply_gate.py`) — prevents bot-to-bot loops. Fails open.
- **Ambient gate** (`ambient_message_gate.py`) — decides with a cheap model
  whether an unaddressed channel message warrants the agent. Fails closed,
  because the cost of a wrong "yes" is an agent talking over people.
- **Scope inference** (`scope_router.py`, `slack_scope_catalog.py`) — infers the
  minimum scopes an agent needs from its own description, then adjudicates
  requested extras against an allowlist. Default-deny.
- **Per-channel configuration** — `agent_channel_configs.py`.
- **Token rotation** — `slack_oauth.py`; operational guidance in
  [docs/runbooks/slack-token-rotation.md](docs/runbooks/slack-token-rotation.md).

**Slack event workers:** use `slack_consumer_worker.py` by default (subprocess
per event, maximum isolation). `warm_worker.py` is an optional, higher-throughput
coroutine-per-event consumer for advanced deployments — do not run both against
the same queue without a deliberate split.

### Sandboxed execution

`executor.py` owns everything around a run — building the script with a prolog
that fixes the working directory and injects only the requested secrets,
redacting secret-shaped output, extracting generated files, retrying transient
acquisition failures, truncating oversized output. *Where* it runs is the
`SandboxProvider`'s business. `sandbox_reaper.py` destroys sandboxes leaked by a
caller that died mid-run.

### Governed access

`api_access.py`, `access_models.py`. Request → approver → time-bounded grant →
expiry warning → revoke → audit trail. Who approves comes from the directory
provider; when none is configured there are no reporting lines to follow and
requests route to administrators, which is the safe direction to fail.

### Automations, workflows and flows

Three different shapes, deliberately:

- **Automations** — cron-scheduled runs. `cron_scheduler.py` gates registration
  so horizontally scaled replicas do not double-fire; `cron_worker_pool.py` and
  `scheduler_worker.py` execute.
- **Workflows** — agent-scoped graphs with human-in-the-loop approval gates
  (`workflow_engine.py`, `api_workflows.py`).
- **Flows** — user-owned chains of skill steps
  (`api_flows.py`, `src/components/computer/`).

### Lab

Describe an application; `app_classifier.py` picks stack and skills,
`api_apps.py` generates it and `node_build.py` builds React apps when needed.
You iterate in chat with live preview, then deploy to a URL on your instance.

### Chat workspace

Multi-tab split-pane chat with an @-mention composer and a context meter. The
**complexity router** (`complexity_router.py`) classifies each turn — greeting,
simple, reasoning, retry-escalation — and routes to a cheap, fast or frontier
model accordingly. Session-per-thread control lives in `chat_session_control.py`.

**In-chat proposals** (`resource_proposals.py`) let the assistant propose a
skill, automation or app mid-conversation; the user approves, AI-edits, dry-runs
or discards it before anything is created.

### Supporting services

`model_pricing.py` (token cost), `token_tracking.py` and `event_log.py`
(accounting), `api_analytics.py` and `api_usage.py` (aggregation),
`ops_alerts.py` and `agent_failure_alerts.py` (alerting), `tenancy.py`
(row-level ownership), `system_catalog.py` (connected systems),
`org_directory.py` (the directory seam), `secret_vault.py` (naming and
validation over the secret backend).

---

## The provider layer

Selected by environment variable. Each accepts a built-in name or a dotted path
to your own class:

```bash
BLOB_STORE=local
BLOB_STORE=acme.providers:ManagedBlobs
```

| Interface | Decides | Built in | Default |
|---|---|---|---|
| `BlobStore` | where files live | `local` | `local` |
| `SecretBackend` | where credential material lives | `local` | `local` |
| `SandboxProvider` | what runs untrusted code | `container`, `subprocess` | `container` |
| `DirectoryProvider` | who the people in the organisation are | `claims`, `file`, `null` | `claims` |
| `RetrievalProvider` | what answers a document search | `local` | `local` |
| `MessageQueue` | what carries work to workers | `inprocess` | `inprocess` |
| `AlertSink` | where operational alerts go | `log`, `webhook` | `log` |

The reference implementations are real, not stubs, and adequate for a
single-node deployment. Two are honest about limits worth knowing:

- the **in-process queue** does not survive a restart — fine for one node, wrong
  for more than one;
- **`subprocess`** execution provides no isolation at all and refuses to start
  without `ALLOW_UNSANDBOXED_EXECUTION=1`;
- **`container`** execution, the default, is ordinary container isolation. One
  throwaway container per run, network off, memory and CPU capped, read-only
  root filesystem, all capabilities dropped, no-new-privileges, no credentials
  mounted. The kernel is shared with the host, so a kernel-level escape is not
  defended against. `SANDBOX_RUNTIME=runsc` moves it onto gVisor if the host has
  it; no such runtime ships here or is installed by `docker compose`. Each
  provider states its own guarantee in `SandboxProvider.isolation`, and that
  string is what the UI shows — so what an operator is told always matches what
  they configured.

See [`backend/providers/__init__.py`](backend/providers/__init__.py) for the
contracts. Each module's docstring explains what the interface is for and why it
is shaped the way it is.

---

## Configuration

`backend/.env.example` documents every option. The ones that shape a deployment:

**Authentication** — any OIDC provider that can put a verifiable JWT in front of
the application. `TEAM_DOMAIN` is the issuer, `POLICY_AUD` the audience. Roles
come from group claims mapped by `ADMIN_GROUPS` / `MEMBER_GROUPS` /
`READONLY_GROUPS`, matched case-insensitively. Providers that emit no groups
still work: by default any authenticated caller is a member, and `ADMIN_EMAILS`
bootstraps the first administrator. Set `REQUIRE_GROUP_MEMBERSHIP=true` once
groups are actually being emitted.

**Naming** — `RESOURCE_PREFIX` prefixes every externally named resource, so
several deployments can share one backing store. `APP_ENV` suffixes those names
for anything but `production`, so environments cannot resolve each other's data.

**Optional data files**, both with worked examples in `data/`:

- `DIRECTORY_FILE` — people, their teams and reporting lines
  (`directory.example.yaml`)
- `SYSTEM_CATALOG_FILE` — connected systems and their risk levels
  (`systems.example.yaml`)

Leave either absent and the features that depend on it degrade rather than break.

---

## Request path

```
browser → identity proxy → API
                            ├─ role from group claims          (auth_policy)
                            ├─ row scoping from the sub claim  (tenancy)
                            └─ handler
                                 ├─ model gateway
                                 ├─ sandbox provider   (untrusted code)
                                 ├─ secret backend     (reference → value)
                                 └─ message queue      → workers
```

Row-level ownership derives from one claim — the subject — and nothing else.
That keeps tenancy independent of the identity provider, so swapping providers
does not change who can see what.

**Background workers**, each runnable independently:

| Worker | Job | Default? |
|---|---|---|
| `slack_consumer_worker.py` | Slack events → isolated subprocess per agent | **Yes** (with `MESSAGE_QUEUE` + DMZ relay at scale) |
| `warm_worker.py` | Slack events → coroutine per event (higher throughput) | Optional — advanced only |
| `scheduler_worker.py` | cron automations | **Yes** (one replica, or two with a leader lease on PostgreSQL) |
| `kb_ingest_worker.py` | knowledge-base document indexing | When agents use knowledge bases |

---

## Repository layout

```
backend/
  providers/          the seven interfaces and their reference implementations
  memory_store/       agent memory: consolidation, policy, episodes, archive
  alembic/            database migrations
  main.py             app assembly, chat loop, tool dispatch
  api_*.py            HTTP routers
  agent_*.py          agent model, execution, memory, scheduling, knowledge base
  skill_*.py          registry, routing, publication, versioning
  workflow_*.py       workflow engine
  org_directory.py    the directory seam
  system_catalog.py   connected-system catalogue
  secret_vault.py     naming and validation over the secret backend

src/
  pages/              one file per screen
  components/         shared UI, plus composer/, computer/, workspace/
  data/skills/        the skill catalogue
  data/               static catalogues (integrations, home templates)

docker/               sandbox and nginx images
deploy/helm/platform/ Helm chart (GKE/EKS)
deploy/k8s/           reference Kubernetes manifests
deploy/terraform/     starter AWS/GCP data-plane examples
docs/runbooks/        operational guides (token rotation, memory export)
backend/providers_reference/  cloud provider sketches for adopters
data/                 example directory and system-catalogue files
```

---

## Operating it

For a full self-hosting checklist (branding, OIDC, directory, Slack, secrets), see
[docs/ADOPTER_GUIDE.md](docs/ADOPTER_GUIDE.md). For GKE, EKS, or other Kubernetes deployments, see
[docs/cloud-deployment.md](docs/cloud-deployment.md).

The `docker compose` stack is a development stack. Before real users touch it:

- put a real identity provider in front of it; set `ENABLE_MOCK_AUTH=false`
- set `SECRET_ENCRYPTION_KEY` to a generated key you have backed up
- install gVisor or Kata on the host and set `SANDBOX_RUNTIME` if your users are
  not all trusted — the default is a plain container sharing the host's kernel,
  and nothing here installs a stronger runtime for you
- reconsider the container-engine socket mount in `docker-compose.yml`; it grants
  the backend control of the host's container engine
- give `RESOURCE_PREFIX` and `APP_ENV` distinct values per environment
- review `backend/api_internal.py`, whose routes are **not** behind the token
  gate and assume they are unreachable from outside your network. If your
  network does not enforce that, they need authentication before you deploy.
- move `SUPER_ACCESS_EMAILS` and `ADMIN_EMAILS` to group membership once you have
  groups; both are bootstrap mechanisms, not a permission model

### Reporting a vulnerability

See [SECURITY.md](SECURITY.md). Report privately — do not test against deployments you do not operate.

---

## Callback and request URLs

Every OAuth flow and Slack's inbound event delivery reach this application by
calling back to a URL you configure once: `APP_BASE_URL`. Nothing here is
per-agent or per-integration — get that one value right and every callback
below resolves correctly.

**It has to be reachable by the third party, not just by your users.** Slack and
Google's servers call these URLs directly. `http://localhost:8080` works for
you in a browser; it does not exist as far as Slack is concerned. For local
development, put a tunnel in front of the backend and set `APP_BASE_URL` to the
tunnel's HTTPS URL instead:

```bash
ngrok http 8080
# or
cloudflared tunnel --url http://localhost:8080
```

Then set `APP_BASE_URL=https://<whatever-that-tunnel-gave-you>` in
`backend/.env` and restart. In production, this is simply wherever you've put
the platform behind a real domain.

### What calls back to what

| Integration | Endpoint | Purpose |
|---|---|---|
| Slack — inbound events | `{APP_BASE_URL}/api/slack/events` | Single endpoint for every registered agent; Slack's `api_app_id` in the payload routes it to the right one. This is the **Request URL** you paste into a Slack app's Event Subscriptions page. |
| Slack — app install | `{APP_BASE_URL}/api/slack-oauth/install-callback` | Where Slack redirects after an admin approves installing an agent's bot. |
| Slack — per-user connect | `{APP_BASE_URL}/api/slack-user-oauth/callback` | Where Slack redirects after a signed-in user connects their own Slack account (separate from an agent's bot token). |
| Google Workspace — per-user connect | `{APP_BASE_URL}/api/google-user-oauth/callback` | Same idea, for a user's own Google account. |

Agents created through the auto-create flow (`api_slack_oauth.py`) have their
Slack app manifest generated automatically, Request URL included — you don't
hand-fill it there. You only need to know these paths when registering a Slack
app by hand (see [`docs/slack-app-setup.md`](docs/slack-app-setup.md)), or when
diagnosing why a callback isn't arriving.

### Running more than one instance

`SLACK_EVENTS_GATEWAY_URL` fronts the Slack events endpoint with a separate
gateway — a relay or queue sitting between Slack and this backend — once you're
running more than one replica and a single instance's Request URL is no longer
enough. **No such gateway ships in this repository**; you would build and
deploy your own, and point it at `slack.events` on whatever `MESSAGE_QUEUE`
provider you've configured. See [`docs/slack-dmz-relay.md`](docs/slack-dmz-relay.md)
for the minimal relay spec (routes, auth, envelope, ACK rules). Leave it unset
for a single instance — the direct `{APP_BASE_URL}/api/slack/events` endpoint
handles everything on its own.

---

## Status

| Area | Maturity |
|------|----------|
| Setup wizard + `npm run setup` | Stable for personal/local and team reconfiguration |
| Workspace, Skills registry, Agents (manual Slack) | Stable |
| Model gateway catalog | Stable — picker reflects your gateway only |
| Lab app builder | Beta — sandbox required; React + vanilla stacks supported |
| Flows / Automations | Beta — scheduling and triggers evolving |
| Slack auto-create + manifest sync | Optional — requires org-admin configuration tokens |
| Admin infra tab | Degrades gracefully off-cluster |

**You must bring:** an OpenAI-compatible model gateway, and (for team deployments) an OIDC-capable identity proxy. Mock auth is for local development only.

Background workers (scheduler, Slack consumers) are optional — see `docker-compose.workers.yml` and [docs/ADOPTER_GUIDE.md](docs/ADOPTER_GUIDE.md#8-background-workers). For Kubernetes, use `deploy/helm/platform/` or `deploy/k8s/` — see [docs/cloud-deployment.md](docs/cloud-deployment.md). `/internal/*` is blocked on the public UI port; workers reach the API on the private network.

CI runs forbidden-pattern checks, TypeScript (`tsc`), and Python unit smoke tests. Before opening a PR:

```bash
npm run lint
cd backend && python -m pytest tests/ -v
cd backend && python -c "import main"
```

See [docs/ADOPTER_GUIDE.md](docs/ADOPTER_GUIDE.md) for the full configuration map.

---

## Licence

Deriv One is released under the [MIT License](LICENSE). Third-party notices:
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
