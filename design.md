# Design: Usage Metering & Billing Engine

## Non-Goal
- No invoicing, proration, or overage billing in the core build

## The Problem
- A multi-tenant service that meters usage (API calls, AI tokens), enforces plan quotas, calculates cost, and syncs subscription state from Stripe test mode, with exactly-once guarantees under retries.

## Layer Sketch
- Layers: HTTP (app.py / routes) -> services (MeterService, BillingService) -> data (models.py, database.py)

## Database Schema
All money is stored as integers. Plan prices are stored in cents. Money is never stored as floats.

### Tenants
- id (PK)
- name
- created_at

### Plans
- id (PK)
- name
- api_call_limit
- ai_token_limit
- price_cents

### Subscriptions
- id (PK)
- tenant_id (FK)
- plan_id (FK)
- status ('active' / 'past_due' / 'canceled')
- stripe_customer_id
- stripe_subscription_id
- current_period_start
- current_period_end

### Usage Events
- id (PK)
- tenant_id (FK)
- type
- quantity
- idempotency_key
- created_at
- Unique constraint on (tenant_id, type, idempotency_key)

### Processed Webhook Events
- stripe_event_id (PK)
- processed_at

## Plans and Quotas

| Plan | API calls/month | AI tokens/month |
|------|-----------------|-----------------|
| Free | 1,000           | 100,000         |
| Pro  | 10,000          | 1,000,000       |

## Quota Rules
- A request is allowed if used + requested <= limit. On Free, call #1,000 succeeds and call #1,001 is rejected.
- 429 Too Many Requests: quota exhausted. The body states which quota, usage, limit, and suggests upgrading. Includes a Retry-After header.
- 402 Payment Required: subscription status blocks the action (past_due or canceled).
- Usage is counted per calendar month.
- Quota is checked before the usage event is recorded.

## API Contract

| Endpoint | Purpose |
|----------|---------|
| `POST /generate` | Billable action. Checks quota, records usage, returns cost. |
| `GET /usage?tenant_id=...` | Returns `{ used, limit, cost }` per usage type. |
| `POST /checkout` | Creates a Stripe Checkout session for the Pro upgrade. |
| `POST /webhooks/stripe` | Receives Stripe events. Verifies the signature, deduplicates, and updates the tenant's plan and status. |

## Idempotency Strategy
- Client sends Idempotency-Key header on POST /generate.
- Handler tries INSERT ... usage_events with that key. If it hits the unique constraint (IntegrityError), fetch and return the original event's result instead of erroring — same response either way.
- Enforcement lives in the database, not in check-then-insert application code, so concurrent retries cannot race.