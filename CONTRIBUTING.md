# Contributing

Thanks for helping improve this project.

## Development setup

1. Clone the repository.
2. Backend dependencies:
   ```bash
   cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   ```
3. Run first-time setup (writes `backend/platform.env`):
   ```bash
   npm install
   npm run setup -- --quick --test-gateway
   ```
   Or use the full interactive wizard: `npm run setup`
4. Start both servers:
   ```bash
   npm run dev:stack
   ```
   Frontend: http://localhost:3000 — backend: http://localhost:8001 (default in `scripts/dev.sh`).

Optional extras:

- `pip install -r requirements-integrations.txt` — Google Workspace OAuth helpers
- `pip install -r requirements-k8s.txt` — Kubernetes infra tab + K8s sandbox
- `pip install -r requirements-gcp.txt` — GCP provider stubs (when available)

Copy `backend/.env.example` → `backend/.env` only when you need values the wizard does not set (database URL, sandbox images, provider overrides).

## Pull requests

- Keep changes focused; one logical change per PR when possible.
- Run checks before pushing:
  ```bash
  npm run lint
  npm test
  npm run api:types   # after changing backend routes; commit src/api/schema.d.ts
  cd backend && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt && .venv/bin/python -m pytest tests/ -v
  bash scripts/check-forbidden-patterns.sh
  ```
- Do not commit secrets, `.env`, `platform.env`, or customer data.
- Update `docs/ADOPTER_GUIDE.md` and `docs/cloud-deployment.md` when changing deployment-facing configuration.

## Code style

- Python: match existing module patterns; explicit error handling for security-sensitive paths.
- TypeScript: `tsc --noEmit` must pass (`npm run lint`).
- Prefer provider interfaces (`backend/providers/`) over hard-coding vendor SDKs in product code.
- Use `APP_NAME` from `app_config` for user-facing copy and LLM prompts — do not hardcode product names.

## Licensing

Contributions are accepted under the repository [LICENSE](LICENSE) (MIT).
