# InvoicePilot: API Contracts (v1)

**Version:** 1.0 · **Date:** 2026-10-20 · **Status:** Ready for implementation

**Related documents:** [System design](../architecture/system-design.md) · [Database design](../design/phase-2.1-database-design.md) · [Threat model](../security/threat-model.md)

This is the contract for the JSON API served by Django REST Framework under `/api/v1/`. The HTMX UI calls the same services through server-rendered views, so the rules here (validation, permissions, error codes) are the rules of the product. An OpenAPI 3.1 schema is generated from the code by `drf-spectacular`. A CI check fails when the generated schema drifts from what this document describes without the document being updated in the same pull request.

---

## 1. Conventions

### 1.1 Transport

| Aspect | Rule |
|---|---|
| Base URL | `https://app.invoicepilot.app/api/v1` |
| Format | JSON, UTF-8. `Content-Type: application/json`. The import upload alone uses `multipart/form-data`. |
| Field names | `snake_case` |
| IDs | UUID strings (UUIDv7) |
| Dates | `YYYY-MM-DD`, a calendar date with no timezone (`issue_date`, `due_date`, `received_on`) |
| Timestamps | RFC 3339 in UTC with `Z`, for example `2026-10-20T14:03:11Z` |
| Money | **Decimal strings**, always with their currency: `"total_amount": "1250.50", "currency": "USD"`. Never JSON numbers. JPY amounts have no decimals (`"120000"`). |
| Empty values | Optional text fields are `""`, not `null`. Optional timestamps and references are `null`. |
| Unknown fields | Rejected with `400 validation_error`, so a typo can't be silently ignored |

### 1.2 Authentication and workspace

- **v1 uses session authentication** (the same login as the web app). Unsafe methods (`POST`, `PATCH`, `DELETE`) require the `X-CSRFToken` header, whose value comes from the `csrftoken` cookie. API keys for third parties are planned separately (system design S1).
- **Workspace:** requests act on the session's active workspace. A client may send `X-Workspace-ID: <uuid>` instead. It must be a workspace the user is a member of, otherwise the response is `404 not_found`.
- **Other workspaces' IDs** always give `404 not_found`, never `403`, so a response can't confirm that a record exists.

### 1.3 Pagination

Lists use cursor pagination, which stays stable while rows are being inserted.

```
GET /api/v1/invoices?page_size=50&cursor=cD0yMDI2LTEwLTE5
```

```json
{
  "results": [ … ],
  "next": "https://app.invoicepilot.app/api/v1/invoices?cursor=cD0yMDI2LTEwLTA4&page_size=50",
  "previous": null
}
```

`page_size` defaults to 25, with a maximum of 100. Lists have no total count, because counting is expensive. Use `/reports/summary` for totals.

### 1.4 Optimistic concurrency

Resources with a `version` field (customers, invoices, reminder policies, email templates) return it in the body and as `ETag: "<version>"`. `PATCH` on these resources **requires** `If-Match: "<version>"`:

| Situation | Response |
|---|---|
| `If-Match` missing | `428 precondition_required` |
| Version is stale | `412 version_conflict`, with the current resource in `current` |
| Version matches | `200`, with the new `version` and `ETag` |

### 1.5 Idempotency

- **`Idempotency-Key` is required** on `POST /invoices/{id}/ledger-entries` and `POST …/reversal`. Use a client-generated UUID, at most 100 characters. It is stored with the ledger entry (unique per workspace).
  - The same key with the same invoice, kind and amount returns `200` with the existing entry and the header `Idempotent-Replayed: true`.
  - The same key with different values returns `409 idempotency_key_reused`.
- `POST /invoices` is naturally idempotent through the unique invoice number: a retry gets `409 duplicate` with the existing invoice's ID in `existing_id`.
- `POST /imports/{id}/commit` and the other state-change actions are idempotent by state: repeating a transition that already happened returns the resource in its current state with `200`.

### 1.6 Errors

Every error is an RFC 9457 problem document with `Content-Type: application/problem+json`:

```json
{
  "type": "https://docs.invoicepilot.app/problems/validation-error",
  "title": "The request is not valid.",
  "status": 400,
  "code": "validation_error",
  "detail": "One or more fields are invalid.",
  "errors": {
    "due_date": ["Due date cannot be before the issue date."],
    "total_amount": ["JPY amounts cannot have decimal places."]
  },
  "request_id": "01JAX4T1R5W0K8V7Q0Z3J2M9PB"
}
```

| HTTP | `code` | When |
|---|---|---|
| 400 | `validation_error` | Field or body validation failed. `errors` maps fields (or `non_field_errors`) to messages. |
| 401 | `not_authenticated` | No valid session |
| 403 | `permission_denied` | The role lacks the action, or the CSRF check failed |
| 403 | `workspace_read_only` | A write while the workspace has no writable subscription (D3) |
| 403 | `workspace_suspended` | The workspace is deactivated |
| 403 | `plan_limit_reached` | The plan's limit would be exceeded. Includes `limit` and `current`. |
| 403 | `email_not_verified` | The action needs a verified user email |
| 403 | `sender_not_verified` | Enabling automation needs a verified sender identity |
| 404 | `not_found` | Doesn't exist **or belongs to another workspace** |
| 405 | `method_not_allowed` | For example `PATCH` on a ledger entry |
| 409 | `duplicate` | A unique field is already used. `existing_id` is included when safe. |
| 409 | `invalid_state` | The transition isn't allowed from the current state. `detail` explains why. |
| 409 | `in_use` | A delete is blocked by history. Deactivate instead. |
| 409 | `idempotency_key_reused` | §1.5 |
| 409 | `possible_duplicate` | A ledger entry looks like a repeat of one recorded in the last 10 minutes. `existing_id` is included. Resend with `"confirm_duplicate": true` to record it anyway (§6). |
| 412 | `version_conflict` | §1.4 |
| 413 | `payload_too_large` | Body over 1 MB, or an upload over 5 MB |
| 415 | `unsupported_media_type` | |
| 428 | `precondition_required` | `If-Match` missing |
| 429 | `rate_limited` | With `Retry-After` |
| 500 | `server_error` | Includes `request_id`. Never a stack trace. |

### 1.7 Rate limits

| Scope | Limit |
|---|---|
| Per user, read (`GET`) | 300 per minute |
| Per user, write | 60 per minute |
| Per workspace, ledger writes | 30 per minute |
| Per workspace, imports | 10 uploads per hour |
| Per workspace, exports | 10 per hour |
| Per user, reminder previews | 30 per minute |

Every response carries `RateLimit-Limit`, `RateLimit-Remaining` and `RateLimit-Reset`. The counters live in Redis. If Redis is unavailable, the limits fail open and an alert fires (system design §13).

### 1.8 Versioning

`/api/v1` is stable. Adding endpoints, optional request fields, response fields or enum values is non-breaking, so clients must ignore unknown fields and handle unknown enum values. Removing or renaming anything, or changing a meaning, requires `/api/v2`. A deprecated endpoint sends `Deprecation` and `Sunset` headers for at least 6 months.

### 1.9 Permissions in the tables below

Each endpoint lists its **action** (from the permission map in system design §6) and whether it needs **write access**. Every endpoint requires an authenticated workspace member. "Write: yes" means it returns `403 workspace_read_only` while the workspace has no writable subscription.

## 2. Endpoint index

| Method | Path | Action | Write | Success |
|---|---|---|---|---|
| GET | `/me` | (member) | no | 200 |
| GET | `/workspace` | `workspace.view` | no | 200 |
| PATCH | `/workspace` | `workspace.update` | yes | 200 |
| GET | `/customers` | `customers.view` | no | 200 |
| POST | `/customers` | `customers.manage` | yes | 201 |
| GET | `/customers/{id}` | `customers.view` | no | 200 |
| PATCH | `/customers/{id}` | `customers.manage` | yes | 200 |
| DELETE | `/customers/{id}` | `customers.manage` | yes | 204 |
| POST | `/customers/{id}/deactivate` | `customers.manage` | yes | 200 |
| POST | `/customers/{id}/reactivate` | `customers.manage` | yes | 200 |
| GET | `/invoices` | `invoices.view` | no | 200 |
| POST | `/invoices` | `invoices.manage` | yes | 201 |
| GET | `/invoices/{id}` | `invoices.view` | no | 200 |
| PATCH | `/invoices/{id}` | `invoices.manage` | yes | 200 |
| POST | `/invoices/{id}/void` | `invoices.manage` | yes | 200 |
| POST | `/invoices/{id}/dispute` | `invoices.manage` | yes | 200 |
| POST | `/invoices/{id}/resolve-dispute` | `invoices.manage` | yes | 200 |
| POST | `/invoices/{id}/pause-reminders` | `invoices.manage` | yes | 200 |
| POST | `/invoices/{id}/resume-reminders` | `invoices.manage` | yes | 200 |
| GET | `/invoices/{id}/ledger-entries` | `invoices.view` | no | 200 |
| POST | `/invoices/{id}/ledger-entries` | `invoices.record_payment` | yes | 201 |
| GET | `/invoices/{id}/ledger-entries/{entry_id}` | `invoices.view` | no | 200 |
| POST | `/invoices/{id}/ledger-entries/{entry_id}/reversal` | `invoices.reverse_payment` | yes | 201 |
| GET | `/invoices/{id}/reminders` | `reminders.view` | no | 200 |
| POST | `/invoices/{id}/reminder-preview` | `reminders.view` | no | 200 |
| GET | `/reminder-deliveries` | `reminders.view` | no | 200 |
| GET | `/reminder-deliveries/{id}` | `reminders.view` | no | 200 |
| GET | `/reminder-policies` | `reminders.view` | no | 200 |
| GET | `/reminder-policies/{id}` | `reminders.view` | no | 200 |
| PATCH | `/reminder-policies/{id}` | `reminders.manage` (+ `reminders.enable_automation` to enable) | yes | 200 |
| POST | `/reminder-policies/{id}/steps` | `reminders.manage` | yes | 201 |
| PATCH | `/reminder-policies/{id}/steps/{step_id}` | `reminders.manage` | yes | 200 |
| DELETE | `/reminder-policies/{id}/steps/{step_id}` | `reminders.manage` | yes | 204 |
| GET | `/email-templates` | `reminders.view` | no | 200 |
| POST | `/email-templates` | `reminders.manage` | yes | 201 |
| GET | `/email-templates/{id}` | `reminders.view` | no | 200 |
| PATCH | `/email-templates/{id}` | `reminders.manage` | yes | 200 |
| DELETE | `/email-templates/{id}` | `reminders.manage` | yes | 204 |
| GET | `/sender-identities` | `reminders.view` | no | 200 |
| POST | `/sender-identities` | `reminders.manage` | yes | 201 |
| PATCH | `/sender-identities/{id}` | `reminders.manage` | yes | 200 |
| POST | `/sender-identities/{id}/resend-verification` | `reminders.manage` | yes | 202 |
| POST | `/sender-identities/{id}/revoke` | `reminders.manage` | yes | 200 |
| GET | `/suppressions` | `reminders.view` | no | 200 |
| POST | `/suppressions` | `reminders.manage` | yes | 201 |
| POST | `/suppressions/{id}/lift` | `reminders.manage` | yes | 200 |
| POST | `/imports` | `imports.manage` | yes | 202 |
| GET | `/imports` | `imports.manage` | no | 200 |
| GET | `/imports/{id}` | `imports.manage` | no | 200 |
| GET | `/imports/{id}/rows` | `imports.manage` | no | 200 |
| POST | `/imports/{id}/commit` | `imports.manage` | yes | 202 |
| POST | `/imports/{id}/cancel` | `imports.manage` | yes | 200 |
| GET | `/imports/template.csv` | `imports.manage` | no | 200 |
| GET | `/reports/summary` | `reports.view` | no | 200 |
| GET | `/reports/aging` | `reports.view` | no | 200 |
| GET | `/exports/invoices.csv` | `reports.export` | no | 200 |
| GET | `/exports/ledger.csv` | `reports.export` | no | 200 |
| GET | `/plans` | (member) | no | 200 |
| GET | `/billing` | `billing.view` | no | 200 |
| POST | `/billing/checkout-session` | `billing.manage` | **no** | 201 |
| POST | `/billing/portal-session` | `billing.manage` | **no** | 201 |
| GET | `/audit-events` | `audit.view` | no | 200 |
| GET | `/schema` | (member) | no | 200 |

Outside `/api/v1`, with no session and no CSRF: `POST /webhooks/stripe`, `POST /webhooks/email` (system design §10), `GET|POST /u/<token>` (unsubscribe), `GET /healthz`, `GET /readyz`.

## 3. Account and workspace

### `GET /me`

```json
{
  "id": "0192a8f0-5b1e-7c3d-9f4a-1b2c3d4e5f60",
  "email": "dana@studio.example",
  "full_name": "Dana Reyes",
  "email_verified": true,
  "memberships": [
    { "organization_id": "0192a8f0-6a00-7d11-8e22-334455667788", "organization_name": "Reyes Studio", "role": "owner" }
  ],
  "active_organization_id": "0192a8f0-6a00-7d11-8e22-334455667788"
}
```

Sign-up, login, logout, email verification and password reset are HTML form flows (system design §5.4). They are not part of the JSON API.

### `GET /workspace`, `PATCH /workspace`

| Field | Type | Writable | Rules |
|---|---|---|---|
| `id` | uuid | no | |
| `name` | string ≤ 200 | yes | Required, not blank |
| `timezone` | string | yes | An IANA name, for example `Europe/Berlin` |
| `default_currency` | string(3) | yes | From the supported list. Applies to new invoices only. |
| `read_only` | boolean | no | `true` while `read_only_since` is set |
| `read_only_since` | timestamp or null | no | |
| `created_at` | timestamp | no | |

`PATCH /workspace` doesn't use `If-Match`: the workspace has no `version` column, so the last write wins. These fields are rarely edited.

## 4. Customers

### Resource

```json
{
  "id": "0192a8f1-0c4d-7e5f-8a9b-0c1d2e3f4a5b",
  "name": "Acme Corp",
  "email": "ap@acme.example",
  "company_name": "Acme Corporation Ltd",
  "notes": "",
  "is_active": true,
  "deactivated_at": null,
  "version": 3,
  "created_at": "2026-10-01T09:12:00Z",
  "updated_at": "2026-10-18T16:40:12Z"
}
```

| Field | Rules |
|---|---|
| `name` | Required, at most 200 characters |
| `email` | Required, a valid address, stored lowercase, **unique in the workspace** (`409 duplicate`) |
| `company_name` | At most 200 characters |
| `notes` | At most 5,000 characters |

### Endpoints

- `GET /customers`. Filters: `q` (matches the start of name or email, case-insensitive), `is_active`. Ordering: `name` (default), `-created_at`.
- `POST /customers` → `201`, with a `Location` header. Checks the plan limit (`403 plan_limit_reached`).
- `PATCH /customers/{id}` with `If-Match`. Writable: `name`, `email`, `company_name`, `notes`.
- `DELETE /customers/{id}` → `204` only if the customer has no invoices. Otherwise `409 in_use`; deactivate instead.
- `POST /customers/{id}/deactivate`, `POST /customers/{id}/reactivate` → `200` with the customer. Scheduled reminders for a deactivated customer are skipped when they come due (`customer_inactive`).

## 5. Invoices

### Resource

```json
{
  "id": "0192a8f2-1111-7222-8333-444455556666",
  "customer": { "id": "0192a8f1-0c4d-7e5f-8a9b-0c1d2e3f4a5b", "name": "Acme Corp", "email": "ap@acme.example" },
  "invoice_number": "INV-1001",
  "currency": "USD",
  "total_amount": "1000.00",
  "balance_due": "400.00",
  "amount_paid": "600.00",
  "issue_date": "2026-09-01",
  "due_date": "2026-10-01",
  "status": "partially_paid",
  "is_overdue": true,
  "days_overdue": 19,
  "description": "September retainer",
  "source": "manual",
  "reminders_paused": false,
  "pause_reason": "",
  "reminder_policy_id": null,
  "disputed_at": null,
  "dispute_note": "",
  "paid_at": null,
  "voided_at": null,
  "next_reminder_at": "2026-10-21T09:00:00Z",
  "version": 4,
  "created_at": "2026-09-01T10:00:00Z",
  "updated_at": "2026-10-15T12:30:00Z"
}
```

| Field | On create | On `PATCH` | Rules |
|---|---|---|---|
| `customer_id` | required | **immutable** | An active customer in this workspace (otherwise `404`) |
| `invoice_number` | required | yes | 1 to 64 characters; unique per workspace, ignoring case (`409 duplicate`) |
| `currency` | optional (defaults to the workspace's) | **immutable** | Supported currency |
| `total_amount` | required | yes | Above 0, at most 2 decimals (0 for JPY). On `PATCH`, the new total must cover what has already been paid, otherwise `409 invalid_state`. |
| `issue_date` | required | yes | |
| `due_date` | required | yes | On or after `issue_date`. A change re-plans reminders. |
| `description` | optional | yes | At most 5,000 characters |
| `reminder_policy_id` | optional | yes | A policy in this workspace, or `null` for the workspace default |
| `balance_due`, `amount_paid`, `status`, `is_overdue`, `days_overdue`, `paid_at`, `voided_at`, `disputed_at`, `next_reminder_at`, `source`, `version` | read-only | read-only | Computed by the services |

`is_overdue` and `days_overdue` are computed with today's date in the workspace timezone. `amount_paid` is `total_amount - balance_due`.

### Endpoints

- `GET /invoices`. Filters: `status` (repeatable), `customer_id`, `overdue=true`, `due_from`, `due_to`, `currency`, `q` (matches the start of the invoice number). Ordering: `-issue_date` (default), `due_date`, `-due_date`, `balance_due`.
- `POST /invoices` → `201`. Checks the monthly invoice limit. Creating an invoice never enables automation. It is planned for reminders only if the workspace's policy is already enabled.
- `PATCH /invoices/{id}` with `If-Match`. A `void` invoice can't be edited (`409 invalid_state`).
- **State actions** return `200` with the invoice:

| Action | Body | Allowed from | Effect |
|---|---|---|---|
| `POST …/void` | `{"reason": "Issued in error"}` (required) | `open`, `partially_paid`, `disputed` | `status = void`. Scheduled reminders are cancelled. Void is final. |
| `POST …/dispute` | `{"note": "Client questions hours"}` | `open`, `partially_paid` | `status = disputed`. Reminders stop. |
| `POST …/resolve-dispute` | `{"note": ""}` | `disputed` | Status is derived from the balance again. Reminders are re-planned. |
| `POST …/pause-reminders` | `{"reason": ""}` | Any status except `void`, `paid` | `reminders_paused = true` |
| `POST …/resume-reminders` | `{}` | Paused invoices | Re-planned. Steps whose time has passed are not sent late. |

A state action that has already been applied (for example, pausing an invoice that is paused) returns `200` with no change.

## 6. Payment ledger

Ledger entries follow [database design §8](../design/phase-2.1-database-design.md#8-payment-ledger-reversals-and-invoice-balance). **They can never be edited or deleted.** `PATCH` and `DELETE` return `405`.

### Resource

```json
{
  "id": "0192a8f3-aaaa-7bbb-8ccc-ddddeeeeffff",
  "invoice_id": "0192a8f2-1111-7222-8333-444455556666",
  "kind": "payment",
  "amount": "600.00",
  "currency": "USD",
  "balance_effect": "-600.00",
  "received_on": "2026-10-10",
  "method": "bank_transfer",
  "reference": "TRX-88231",
  "note": "",
  "source": "api",
  "reverses_id": null,
  "reversed_by_id": null,
  "reversal_reason": "",
  "recorded_by": { "id": "0192a8f0-5b1e-7c3d-9f4a-1b2c3d4e5f60", "email": "dana@studio.example" },
  "created_at": "2026-10-10T15:22:41Z"
}
```

`currency` is the invoice's currency, repeated for convenience. `reversed_by_id` is the reversal entry's ID if this entry has been reversed.

### `POST /invoices/{id}/ledger-entries`

Headers: `Idempotency-Key` (required), `X-CSRFToken`.

```json
{ "kind": "payment", "amount": "600.00", "received_on": "2026-10-10", "method": "bank_transfer", "reference": "TRX-88231", "note": "" }
```

| Field | Rules |
|---|---|
| `kind` | `payment`, `credit` or `refund`. `reversal` is not accepted here; use the reversal endpoint. |
| `amount` | Above 0, at most 2 decimals (0 for JPY) |
| `received_on` | Not more than 1 day in the future in the workspace timezone |
| `method` | `bank_transfer`, `card`, `cash`, `cheque`, `online`, `other`, or `""` |
| `reference` | At most 128 characters |
| `note` | At most 2,000 characters |
| `confirm_duplicate` | Optional boolean, default `false`. Set it to `true` to record an entry that the possible-duplicate guard flagged. |

Responses:

| Result | Response |
|---|---|
| Recorded | `201` with `{"entry": {…}, "invoice": {…}}`, so the client gets the new balance and status in one call |
| Replay of the same key | `200`, the same body, `Idempotent-Replayed: true` |
| Same invoice, kind, amount and `received_on` as an entry from the last 10 minutes, under a different key | `409 possible_duplicate` with `existing_id`, unless `confirm_duplicate` is `true` (threat model T7) |
| Payment or credit greater than `balance_due` | `409 invalid_state`, "Amount exceeds the outstanding balance of 400.00 USD." |
| Refund greater than the amount paid | `409 invalid_state` |
| Invoice is `void` | `409 invalid_state` |
| Missing `Idempotency-Key` | `400 validation_error` |

### `POST /invoices/{id}/ledger-entries/{entry_id}/reversal`

Headers: `Idempotency-Key` (required), `X-CSRFToken`.

```json
{ "reason": "Recorded against the wrong invoice", "received_on": "2026-10-20" }
```

| Result | Response |
|---|---|
| Reversed | `201` with `{"entry": {reversal}, "invoice": {…}}`. The reversal's `amount` equals the original's, and its `balance_effect` is the negative of the original's. |
| Entry already reversed | `409 invalid_state`, with the existing reversal's ID in `existing_id` |
| Entry is itself a reversal | `409 invalid_state`, "A reversal cannot be reversed. Record a new payment instead." |
| The resulting balance would leave 0…total | `409 invalid_state`, "Reverse the later entries first." |
| `reason` empty | `400 validation_error` |

`GET /invoices/{id}/ledger-entries` returns entries oldest first (cursor pagination), including reversals.

## 7. Reminders

### Delivery resource

```json
{
  "id": "0192a8f4-0000-7111-8222-333344445555",
  "invoice_id": "0192a8f2-1111-7222-8333-444455556666",
  "step": { "id": "0192a8f0-7000-7000-8000-000000000003", "offset_days": 7, "position": 2 },
  "invoice_due_date": "2026-10-01",
  "scheduled_for": "2026-10-08T07:00:00Z",
  "status": "sent",
  "skip_reason": "",
  "recipient_email": "ap@acme.example",
  "subject": "Reminder: invoice INV-1001 is 7 days overdue",
  "body_text": "Hi Acme Corp, …",
  "attempt_count": 1,
  "sent_at": "2026-10-08T07:00:04Z",
  "delivered_at": "2026-10-08T07:00:09Z",
  "attempts": [
    { "attempt_number": 1, "started_at": "2026-10-08T07:00:03Z", "finished_at": "2026-10-08T07:00:04Z", "outcome": "accepted", "provider_status_code": 200, "error_message": "" }
  ]
}
```

`attempts` appears only on `GET /reminder-deliveries/{id}`. `provider_message_id` and `provider_request_id` are not exposed.

- `GET /invoices/{id}/reminders`: deliveries for one invoice, in `scheduled_for` order.
- `GET /reminder-deliveries`: the activity feed. Filters: `status` (repeatable), `invoice_id`, `from`, `to` (on `scheduled_for`). Ordering: `-scheduled_for`.
- `POST /invoices/{id}/reminder-preview`, body `{"step_id": "…"}`: renders the subject and body that step would send now, without sending or saving anything. Returns `{"recipient_email", "subject", "body_text", "would_send": false, "blocked_by": ["policy_disabled", "sender_unverified"]}`, so the user can see why a reminder would be skipped.

### Reminder policy

| Field | Writable | Rules |
|---|---|---|
| `id`, `version` | no | |
| `name` | yes | At most 100 characters, unique per workspace |
| `is_enabled` | yes | Setting it to `true` needs `reminders.enable_automation`, a verified user email (`403 email_not_verified`) and a verified default sender (`403 sender_not_verified`) |
| `is_default` | no | |
| `send_window_start`, `send_window_end` | yes | `HH:MM`, workspace-local time, start before end |
| `send_on_weekends` | yes | |
| `steps` | no (use the step endpoints) | Ordered by `position` |

`PATCH /reminder-policies/{id}` requires `If-Match`, writes an audit event, and re-plans the workspace's deliveries in the background.

**Steps:** `POST /reminder-policies/{id}/steps`, body `{"offset_days": 21, "template_id": "…", "position": 4}`.
- `offset_days` runs from −30 to 365 and is unique in the policy. `template_id` must be a template in this workspace.
- `PATCH` accepts `offset_days`, `template_id`, `position` and `is_active`.
- `DELETE` returns `204` only for a step with no deliveries. Otherwise `409 in_use`; set `is_active: false` instead.

### Email templates

| Field | Rules |
|---|---|
| `name` | At most 100 characters, unique per workspace |
| `subject` | At most 200 characters. No line breaks. |
| `body_text` | At most 10,000 characters, plain text |
| `placeholders_used` | Read-only |

Placeholders use `{{name}}` from a fixed allow-list: `customer_name`, `company_name`, `invoice_number`, `issue_date`, `due_date`, `days_overdue`, `total_amount`, `balance_due`, `currency`, `sender_name`. An unknown placeholder is rejected at save time with `400 validation_error`. Rendering is plain substitution, not a template engine (threat model T8). `DELETE` is blocked with `409 in_use` while any step uses the template.

### Sender identities

- `POST /sender-identities`, body `{"from_name": "Dana Reyes", "reply_to_email": "dana@studio.example"}` → `201` with `status: "pending"`, and a verification link is emailed to `reply_to_email`.
- `from_name` is at most 100 characters and may not contain `@`, `<`, `>`, `"` or line breaks (threat model T8).
- `PATCH` accepts only `from_name` and `is_default`. Changing the address means creating a new identity.
- `POST …/resend-verification` → `202`, limited to 3 per hour.
- `POST …/revoke` → `200`, `status: "revoked"`. Reminders that would use it are skipped with `sender_unverified`.

### Suppressions

- `GET /suppressions` returns this workspace's suppressions, **plus** platform-wide suppressions only for addresses that belong to this workspace's customers. A workspace never sees the platform list in general.
- `POST /suppressions`, body `{"email": "…"}` → a `manual` suppression scoped to the workspace. Repeating it for an address that is already suppressed returns `200` with the existing entry.
- `POST /suppressions/{id}/lift` → `200` for workspace-scoped entries. Platform-wide entries (hard bounce, complaint) return `403 permission_denied`, "Contact support"; only staff can lift them.

## 8. Imports

1. `POST /imports` (`multipart/form-data`, field `file`): a `.csv` file of at most 5 MB, UTF-8 (a BOM is accepted). → `202` with the batch in `uploaded`. Validation runs in the background.
2. Poll `GET /imports/{id}` until `status` is `validated` or `failed`. The HTMX UI polls every 2 seconds with `hx-trigger="every 2s"` and stops once validation finishes.
3. Review `GET /imports/{id}/rows?status=error`.
4. `POST /imports/{id}/commit` → `202`, and the batch moves to `committing` and then `committed`. Committing from any status other than `validated` returns `409 invalid_state`. A commit that is already running or finished returns the batch with `200`.

```json
{
  "id": "0192a8f5-0000-7000-8000-000000000001",
  "original_filename": "october.csv",
  "status": "validated",
  "row_count": 120,
  "valid_row_count": 117,
  "error_row_count": 2,
  "duplicate_row_count": 1,
  "previously_imported_at": "2026-10-02T08:00:00Z",
  "committed_at": null,
  "error_message": "",
  "created_at": "2026-10-20T10:00:00Z"
}
```

`previously_imported_at` is set when a file with the same SHA-256 was committed before in this workspace. The UI shows it as a warning.

**Row resource:** `{"row_number": 7, "status": "error", "data": {…}, "errors": [{"column": "due_date", "message": "Not a valid date (expected YYYY-MM-DD)."}], "invoice_id": null}`.

**Columns** (header row required, order free, names case-insensitive; `GET /imports/template.csv` returns a sample):

| Column | Required | Rules |
|---|---|---|
| `invoice_number` | yes | As for invoices. A duplicate within the file or the workspace makes the row a `duplicate`. |
| `customer_email` | yes | Matches an existing customer, or creates one |
| `customer_name` | for new customers | |
| `issue_date`, `due_date` | yes | `YYYY-MM-DD` |
| `total_amount` | yes | A plain decimal: no currency symbols and no thousands separators |
| `currency` | no | Defaults to the workspace's currency |
| `amount_paid` | no | Recorded as a `payment` ledger entry with `source = csv_import` |
| `description` | no | |

Limits: at most 5,000 data rows, 30 columns, 10,000 characters per field, and 1 MB per line. Other columns are ignored and listed in a warning. Parsing rules are in threat model T5.

## 9. Reports and exports

### `GET /reports/summary`

Totals by currency. Different currencies are never added together.

```json
{
  "as_of": "2026-10-20",
  "currencies": [
    {
      "currency": "USD",
      "outstanding": "18450.00",
      "overdue": "6200.00",
      "open_invoice_count": 14,
      "overdue_invoice_count": 5,
      "collected_last_30_days": "9100.00"
    }
  ],
  "reminders_sent_this_month": 37,
  "plan_reminder_limit": 500
}
```

### `GET /reports/aging`

Buckets by days overdue, per currency: `current` (not yet due), `1_30`, `31_60`, `61_90`, `over_90`. Each bucket has `amount` and `count`. Optional filter: `customer_id`.

### `GET /exports/invoices.csv`, `GET /exports/ledger.csv`

A streamed `text/csv; charset=utf-8` response with a BOM, so Excel detects UTF-8, and `Content-Disposition: attachment`. Filters match the matching list endpoints. Allowed in read-only workspaces. **Every text cell is escaped against formula injection** (threat model T6). Amounts are written as plain decimals. Every export writes an audit event `export.created`.

## 10. Billing

- `GET /plans` lists the active plans: `code`, `name`, the limits, and a display price taken from Stripe and cached.
- `GET /billing`:
  ```json
  {
    "plan": { "code": "solo", "name": "Solo" },
    "status": "active",
    "current_period_end": "2026-11-01T00:00:00Z",
    "cancel_at_period_end": false,
    "read_only": false,
    "usage": { "customers": { "used": 31, "limit": 100 }, "invoices_this_month": { "used": 12, "limit": 100 }, "reminders_this_month": { "used": 37, "limit": 500 } }
  }
  ```
- `POST /billing/checkout-session`, body `{"plan_code": "solo"}` → `201` with `{"url": "https://checkout.stripe.com/…"}`. **Allowed while read-only**, because that is how a workspace becomes writable. A workspace that already has a live subscription gets `409 invalid_state` and should use the portal.
- `POST /billing/portal-session` → `201` with `{"url": "https://billing.stripe.com/…"}`. Allowed while read-only.

Subscription state changes **only** through verified Stripe webhooks, never from the redirect back after checkout. After the redirect, the UI polls `GET /billing` until the status changes.

## 11. Audit events

`GET /audit-events`. Filters: `action` (prefix, for example `payment.`), `target_type`, `target_id`, `from`, `to`. Ordering: `-created_at`.

```json
{
  "id": 918273,
  "created_at": "2026-10-20T09:15:02Z",
  "actor_type": "user",
  "actor": { "id": "0192a8f0-5b1e-7c3d-9f4a-1b2c3d4e5f60", "email": "dana@studio.example" },
  "action": "payment.reversed",
  "target_type": "invoices.paymentrecord",
  "target_id": "0192a8f3-aaaa-7bbb-8ccc-ddddeeeeffff",
  "changes": {},
  "metadata": { "reason": "Recorded against the wrong invoice" }
}
```

IP addresses and request IDs stay in the stored `metadata`, but this endpoint does not return them. `admin.support_access` events are included, so owners can see when support staff opened their data.

## 12. Contract tests

| Test | Checks |
|---|---|
| `test_openapi_schema_matches_snapshot` | The generated schema equals the committed `docs/api/openapi.yaml`. An intended change updates both in the same pull request. |
| `test_every_endpoint_declares_action_and_write_flag` | Walks the URL resolver. Every API view has a `required_action` and a `requires_write_access` attribute. |
| `test_cross_workspace_ids_return_404` | Parametrized over every `{id}` route and method (threat model T1 and T2) |
| `test_read_only_workspace_blocks_writes` | Every "Write: yes" endpoint returns `403 workspace_read_only`. Every "no" endpoint still works. |
| `test_errors_are_problem_json` | One case per error code in §1.6 |
| `test_money_is_serialized_as_strings` | No JSON number appears in any money field |
| `test_ledger_idempotency_replay_and_conflict` | §1.5 |
| `test_patch_requires_if_match` | 428 and 412 behaviour |
| `test_unknown_fields_rejected` | §1.1 |
