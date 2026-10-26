from uuid import UUID

from fastapi import Depends, FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from meter_service import MeterService, QuotaExceeded, SubscriptionInactive, TenantNotFound

app = FastAPI(title="Usage Metering & Billing Engine")

class GenerateRequest(BaseModel):
    tenant_id: UUID
    input_tokens: int = Field(0, ge=0)
    cached_input_tokens: int = Field(0, ge=0)     # This is for part 4
    output_tokens: int = Field(0, ge=0)
    reasoning_tokens: int = Field(0, ge=0)


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