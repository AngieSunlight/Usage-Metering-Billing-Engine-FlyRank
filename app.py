from uuid import UUID
import json
import stripe
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from fastapi.concurrency import run_in_threadpool

from billing_service import AlreadyOnPro, BillingService
from database import get_db, settings   # settings added to the existing import
from meter_service import MeterService, QuotaExceeded, SubscriptionInactive, TenantNotFound

app = FastAPI(title="Usage Metering & Billing Engine")

class GenerateRequest(BaseModel):
    tenant_id: UUID
    input_tokens: int = Field(0, ge=0)
    cached_input_tokens: int = Field(0, ge=0)     # This is for part 4
    output_tokens: int = Field(0, ge=0)
    reasoning_tokens: int = Field(0, ge=0)

class CheckoutRequest(BaseModel):
    tenant_id: UUID

@app.post("/generate")
def generate(
    body: GenerateRequest,
    # FASTAPI reads idempotency_key
    idempotency_key: str = Header(..., min_length=1, max_length=255),
    db: Session = Depends(get_db),
):
    # 
    breakdown = body.model_dump(exclude={"tenant_id"})
    total_tokens = sum(breakdown.values())

    # Every call uses 1 API call
    quantities = {"api_call": 1}
    if total_tokens > 0:
        quantities["ai_tokens"] = total_tokens


    # Transaltes the service's exceptinos into HTTP status codes
    try:
        return MeterService(db).record(body.tenant_id, idempotency_key, quantities, breakdown)
    except TenantNotFound:
        return JSONResponse(status_code=404, content={"error": "tenant_not_found",
                                                      "message": "No subscription found for this tenant."})
    except SubscriptionInactive as e:
        # 402 --> a payment/subscription problem
        return JSONResponse(status_code=402, content={
            "error": "payment_required",
            "message": f"Your subscription is {e.status}. Update your payment details or upgrade to continue.",
        })
    except QuotaExceeded as e:
        # 429 --> Flagged when issue of usage
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(e.retry_after)},
            content={
                "error": "quota_exceeded",
                "quota": e.usage_type,
                "used": e.used,
                "limit": e.limit,
                "requested": e.requested,
                "message": f"Monthly {e.usage_type} quota reached ({e.used}/{e.limit}). "
                           f"Upgrade to Pro for higher limits, or retry after the Retry-After seconds.",
            },
        )


@app.post("/checkout")
def checkout(body: CheckoutRequest, db: Session = Depends(get_db)):
    try:
        return BillingService(db).create_checkout_session(body.tenant_id)
    except TenantNotFound:
        return JSONResponse(status_code=404, content={"error": "tenant_not_found",
                                                      "message": "No subscription found for this tenant."})
    except AlreadyOnPro:
        return JSONResponse(status_code=409, content={"error": "already_pro",
                                                      "message": "This tenant is already on the Pro plan."})
    except stripe.StripeError as e:
        return JSONResponse(status_code=502, content={"error": "stripe_error", "message": str(e)})


@app.get("/checkout/success")
def checkout_success():
    return {"message": "Payment received. Your plan updates as soon as Stripe confirms it."}


@app.get("/checkout/cancel")
def checkout_cancel():
    return {"message": "Checkout cancelled. No changes were made."}


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()          # RAW bytes, exactly what Stripe signed
    signature = request.headers.get("stripe-signature")
    try:
        if not signature:
            raise ValueError("missing signature header")
        # Raises if the signature doesn't match or the timestamp is stale.
        stripe.Webhook.construct_event(payload, signature, settings.stripe_webhook_secret)
    except (ValueError, stripe.SignatureVerificationError):
        return JSONResponse(status_code=400, content={"error": "invalid_signature"})

    # The payload is now verified, so parse it ourselves into a plain dict.
    event = json.loads(payload)
    # The DB work is synchronous, so run it off the event loop.
    result = await run_in_threadpool(BillingService(db).handle_event, event)
    return {"status": result}