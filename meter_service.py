from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import Subscription, UsageEvent


# The custom exceptions are raised instead of returning HTTP responses

class TenantNotFound(Exception):
    pass


class SubscriptionInactive(Exception):
    def __init__(self, status: str):
        self.status = status  # past_due, canceled, used in the 402 message


class QuotaExceeded(Exception):
    # Carries eveything the 429 body needs to explain the rejection
    def __init__(self, usage_type, used, limit, requested, retry_after):
        self.usage_type = usage_type
        self.used = used
        self.limit = limit
        self.requested = requested
        self.retry_after = retry_after

# Gets when the month starts and when the next one begins since quotas are per month

def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_month_start(now: datetime) -> datetime:
    start = month_start(now)
    if start.month == 12:
        # Since December --> January (the nest year)
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


class MeterService:
    def __init__(self, db: Session):
        # DB session passed in
        self.db = db

    def record(self, tenant_id, idempotency_key, quantities: dict, token_breakdown: dict) -> dict:
        try:
            return self._record(tenant_id, idempotency_key, quantities, token_breakdown)
        except BaseException:
            self.db.rollback()  # releases the row lock when failure
            raise

    def _record(self, tenant_id, key, quantities, token_breakdown):
        now = datetime.now(timezone.utc)

        # Lock's row for tenant's subscription so metering is serialized per tenant
        sub = (
            self.db.query(Subscription)
            .filter(Subscription.tenant_id == tenant_id)
            .with_for_update()
            .one_or_none()
        )
        if sub is None:
            raise TenantNotFound()

        # Return the OG result
        existing = self._events_for_key(tenant_id, key)
        if existing:
            return self._result(key, existing)

        if sub.status != "active":
            raise SubscriptionInactive(sub.status)

        # Checks the quota
        limits = {
            "api_call": sub.plan.api_call_limit,
            "ai_tokens": sub.plan.ai_token_limit,
        }
        since = month_start(now)
        retry_after = int((next_month_start(now) - now).total_seconds())
        for usage_type, requested in quantities.items():
            used = self._used(tenant_id, usage_type, since)
            if used + requested > limits[usage_type]:
                raise QuotaExceeded(usage_type, used, limits[usage_type], requested, retry_after)

        
        try:
            for usage_type, qty in quantities.items():
                self.db.add(UsageEvent(
                    tenant_id=tenant_id,
                    type=usage_type,
                    quantity=qty,
                    idempotency_key=key,
                    meta=token_breakdown if usage_type == "ai_tokens" else {},
                ))
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            return self._result(key, self._events_for_key(tenant_id, key))

        return self._result(key, self._events_for_key(tenant_id, key))

    def _events_for_key(self, tenant_id, key):
        return (
            self.db.query(UsageEvent)
            .filter(UsageEvent.tenant_id == tenant_id, UsageEvent.idempotency_key == key)
            .all()
        )

    def _used(self, tenant_id, usage_type, since):
        return (
            self.db.query(func.coalesce(func.sum(UsageEvent.quantity), 0))
            .filter(
                UsageEvent.tenant_id == tenant_id,
                UsageEvent.type == usage_type,
                UsageEvent.created_at >= since,
            )
            .scalar()
        )

    @staticmethod
    def _result(key, events):
        return {"idempotency_key": key, "recorded": {e.type: e.quantity for e in events}}