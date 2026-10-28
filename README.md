# InvoicePilot

Automated invoice tracking and payment follow-up for freelancers and consultants.

**Stack:** Python 3.13 · Django 5.2 LTS · Django REST Framework · PostgreSQL 17 · pytest · Ruff · Docker / Docker Compose · uv

**Status:** foundation phase. Accounts (sign-up, login, email verification, password reset), workspaces (organizations, owner membership, tenant resolution) and customer management are implemented. The schema for invoices, reminders, billing, imports, audit and webhooks exists from the data-model phase ([database design](docs/design/phase-2.1-database-design.md)), but none of those features have views, services or APIs yet.

## Design documents

| Document | Contract for |
|---|---|
| [System design](docs/architecture/system-design.md) | Modules, request lifecycle, transactions, deployment |
| [Database design](docs/design/phase-2.1-database-design.md) | Tables, constraints, tenant isolation, retention |
| [API contracts](docs/api/api-contracts.md) | `/api/v1` endpoints, errors, conventions |
| [Threat model](docs/security/threat-model.md) | Security controls and the tests that prove them |

## Repository layout

```
backend/
  manage.py
  config/
    settings/        base.py, local.py, test.py, production.py
    env.py           typed environment-variable readers
    urls.py  wsgi.py  asgi.py
  apps/
    core/            base models, UUIDv7, RequestContext, domain errors, problem-JSON, throttling, health
    accounts/        custom User (UUID PK, normalized unique email), auth flows
    organizations/   Organization, Membership, permissions, selectors, services, TenantMiddleware, API
    customers/       Customer, selectors, services, forms, HTMX views, API
    invoices/ reminders/ billing/ imports/ audit/ webhooks/ retention/   schema only, for now
  static/vendor/     HTMX, vendored at a pinned version (checksum-tested)
  templates/         base.html
  tests/             pytest suite (PostgreSQL)
docs/                design documents
Dockerfile           dev and production targets
compose.yaml         local stack: PostgreSQL 17, Mailpit, web
Makefile             developer commands (run `make`)
```

## Getting started

### Option A: Docker Compose (recommended)

Requires Docker with Compose v2.24 or newer.

```bash
make env        # creates .env from .env.example
make up         # builds and starts db, mailpit and web
```

- App: http://localhost:8000 (sign up at `/signup/`)
- Emails (verification, password reset): http://localhost:8025 (Mailpit)
- Tests in the container: `make compose-test`

### Option B: on your machine

Requires [uv](https://docs.astral.sh/uv/) and a PostgreSQL 17 server you can create databases on.

```bash
make install    # uv sync --locked, plus the pre-commit hooks
make env        # then edit POSTGRES_* in .env to point at your server
make run        # migrate, create the cache table, start http://localhost:8000
```

Emails print to the console by default.

## Developer commands

| Command | What it does |
|---|---|
| `make install` | Install dependencies from `uv.lock` and the git pre-commit hooks |
| `make run` | Migrate and start the dev server |
| `make migrate` / `make makemigrations` | Apply / create migrations |
| `make test` | Run pytest (needs PostgreSQL; SQLite is not supported) |
| `make lint` / `make format` | Ruff check and format check / auto-fix |
| `make check` | Django system checks and the missing-migrations check |
| `make ci` | Everything CI runs: lint, checks, tests |
| `make up` / `make down` / `make logs` | Manage the Compose stack |
| `make superuser` / `make shell` | Create an admin user / Django shell |

Commands run on the host load `.env` automatically when it exists. Tests always use `config.settings.test`, whatever `DJANGO_SETTINGS_MODULE` says.

## Configuration

All configuration comes from environment variables (12-factor). [`.env.example`](.env.example) documents every variable, with its default and whether production requires it.

| Settings module | Used by | Notes |
|---|---|---|
| `config.settings.local` | `manage.py`, Compose | `DEBUG` on, fixed insecure key, console or Mailpit email |
| `config.settings.test` | pytest, CI | PostgreSQL, fast MD5 hasher, in-memory email and cache |
| `config.settings.production` | `wsgi`/`asgi`, the production image | Refuses to start without `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` and `SITE_URL`. HTTPS redirect, HSTS, secure cookies. |

`.env` is git-ignored. Secrets are never committed: staging and production read them from the platform's secret store.

## What's implemented

### Accounts

- **User model:** UUIDv7 primary key; email is the login, normalized to lowercase, unique, and backed by a database `CHECK`. `AUTH_USER_MODEL = "accounts.User"` has been set since the first migration.
- **Sign-up** (`/signup/`): plain form, Django password validators (minimum 12 characters, common, similar or all-digit passwords rejected). An existing email gets the same "check your inbox" response, and its owner gets a notice, so the form never reveals which accounts exist.
- **Email verification** (`/verify-email/<token>/`): token signed with Django's `signing` module, purpose-salted, valid for 48 hours, and bound to the current email address. Opening the link (GET) only shows a confirm button; the POST verifies, so mail scanners can't verify on the user's behalf.
- **Login and logout:** Django's session auth. The session key rotates on login. `next` is validated. Logout is POST-only. Throttling: 5 failures per account per 15 minutes and 30 per IP per 10 minutes. Unknown emails and wrong passwords get the same message.
- **Password reset:** Django's token generator and views. Links expire after 1 hour and are built from `SITE_URL`, never the request host. Unknown emails get the same response. A reset signs out every other session.
- **Hashing:** Argon2id first, with PBKDF2 kept so older hashes upgrade on login.

Sign-up, login, verification and reset are HTML form flows, as [API contracts §3](docs/api/api-contracts.md#3-account-and-workspace) specifies. They are not JSON endpoints.

### Workspaces

- `Organization` is the tenant root. `Membership` has a conditional unique constraint, so a workspace has at most one owner (`UNIQUE (organization_id) WHERE role = 'owner'`), plus `UNIQUE (organization_id, user_id)`.
- `services.create_workspace` creates the workspace and its owner membership in one transaction.
- `TenantMiddleware` resolves the active workspace on every request, from the user's own memberships only: the `X-Workspace-ID` header (API), then the session, then the user's sole workspace. A workspace the user doesn't belong to always returns 404, never 403.
- `permissions.require(ctx, action)` checks the role-to-action map. Suspended workspaces get 403, and read-only workspaces (no active subscription) refuse writes.
- HTML: `/app/` (dashboard), `/app/workspaces/` (picker), `/app/workspaces/new/`.
- API: `GET /api/v1/me`, `GET`/`PATCH /api/v1/workspace`. Errors are RFC 9457 problem documents, and unknown or read-only body fields are rejected.

### Customers

- **Model:** name, email, company, notes, active state and a `version` column for optimistic locking. Database `CHECK`s back every rule: email lowercase and unique per workspace, non-blank name and email, notes at most 5,000 characters, `deactivated_at` set exactly when inactive, `version >= 1`.
- **Services** (`customers.services`) are the only writers. Each checks `customers.manage` and write access itself, so the HTML app, the API and future imports get the same rules. Creating a customer checks the plan's `max_customers` under a per-workspace advisory lock (`billing.limits.check_plan_limit`). Deactivated customers count toward the limit.
- **Edits** compare `version`: a stale save is refused with the current record instead of overwriting someone else's change. Deactivate and reactivate are idempotent. Delete works only for customers with no invoices; otherwise it's `409 in_use`, and you deactivate instead.
- **Tenant isolation:** every read starts from `Customer.objects.for_organization(ctx.organization)`. Another workspace's customer ID is a 404 on every HTML and API route, and the tests walk each route and method to prove it.
- **HTML** (`/app/customers/`): list with search and status filter (HTMX refreshes only the results, and it works as a plain form without JavaScript), page-number pagination, add, edit (hidden `version`), deactivate/reactivate (HTMX swaps the row), and delete with confirmation.
- **API** ([API contracts §4](docs/api/api-contracts.md#4-customers)): `GET`/`POST /api/v1/customers` (cursor pagination, `q`, `is_active`, `ordering`), `GET`/`PATCH`/`DELETE /api/v1/customers/{id}`, `POST …/deactivate`, `POST …/reactivate`. `PATCH` requires `If-Match: "<version>"` (`428` without it, `412 version_conflict` with `current` when it's stale). Responses carry an `ETag`.
- **HTMX 2.0.11** is vendored in `backend/static/vendor/` and configured with a `<meta>` tag (no inline script): same-origin requests only, no eval, no history cache. Every HTMX request sends the CSRF token through `hx-headers`.

### Health

- `GET /healthz`: liveness. Doesn't touch the database.
- `GET /readyz`: readiness. Runs `SELECT 1` and returns 503 if PostgreSQL is unreachable.

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push to `main` and on every pull request:

1. **Ruff:** lint and format check.
2. **Checks, migrations and tests** against a PostgreSQL 17 service container:
   - `manage.py check --fail-level WARNING`
   - `makemigrations --check --dry-run`, which **fails the build on missing migrations**
   - `migrate` on an empty database
   - `check --deploy` with production settings
   - `pytest`
3. **Secret scan:** gitleaks over the full git history.

The workflow stores no secrets. The CI database uses `trust` auth inside the job, and the deploy check generates a throwaway key for each run.

Pre-commit (`make install` sets it up) runs the same Ruff version, standard hygiene hooks, gitleaks, and the missing-migrations check.
