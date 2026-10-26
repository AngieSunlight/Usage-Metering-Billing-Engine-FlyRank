from datetime import datetime, timezone
from uuid import UUID

import stripe
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import settings
from meter_service import TenantNotFound
from models import Plan, ProcessedWebhookEvent, Subscription

stripe.api_key = settings.stripe_secret_key

STATUS_MAP = {
    "active": "active", "trialing": "active",
    "past_due": "past_due", "unpaid": "past_due",
    "canceled": "canceled", "incomplete_expired": "canceled",
}


class AlreadyOnPro(Exception):
    pass


def _ts(value):
    return datetime.fromtimestamp(value, tz=timezone.utc) if value else None


def _period(sub_obj):
    items = (sub_obj.get("items") or {}).get("data") or [{}]
    start = items[0].get("current_period_start", sub_obj.get("current_period_start"))
    end = items[0].get("current_period_end", sub_obj.get("current_period_end"))
    return _ts(start), _ts(end)


class BillingService:
    def __init__(self, db: Session):
        self.db = db

    def create_checkout_session(self, tenant_id) -> dict:
        sub = self.db.query(Subscription).filter(Subscription.tenant_id == tenant_id).one_or_none()
        if sub is None:
            raise TenantNotFound()
        if sub.plan.name == "pro" and sub.status == "active":
            raise AlreadyOnPro()

        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": settings.stripe_price_id, "quantity": 1}],
            # Two ways for webhooks to find our tenant again:
            client_reference_id=str(tenant_id),  # during the session
            subscription_data={"metadata": {"tenant_id": str(tenant_id)}},  # if you have a subscription
            success_url=f"{settings.app_base_url}/checkout/success",
            cancel_url=f"{settings.app_base_url}/checkout/cancel",
        )
        return {"checkout_url": session.url}

    #For the WEBHOOKS
    def handle_event(self, event: dict) -> str:
        # Stripe event id is the primary key.
        try:
            self.db.add(ProcessedWebhookEvent(stripe_event_id=event["id"]))
            self.db.flush()
        except IntegrityError:
            self.db.rollback()
            return "duplicate"

        try:
            obj = event["data"]["object"]
            kind = event["type"]
            if kind == "checkout.session.completed":
                self._on_checkout_completed(obj)
            elif kind in ("customer.subscription.created", "customer.subscription.updated"):
                self._on_subscription_updated(obj)
            elif kind == "customer.subscription.deleted":
                self._on_subscription_deleted(obj)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return "processed"

    def _find_subscription(self, stripe_sub_id, tenant_id_str):
        # Claude Wrote this function because I was confused: Locks the row so a webhook can't race with a /generate call.
        q = self.db.query(Subscription).with_for_update()
        sub = None
        if stripe_sub_id:
            sub = q.filter(Subscription.stripe_subscription_id == stripe_sub_id).one_or_none()
        if sub is None and tenant_id_str:
            try:
                sub = q.filter(Subscription.tenant_id == UUID(tenant_id_str)).one_or_none()
            except ValueError:
                return None
        return sub

    def _plan(self, name):
        return self.db.query(Plan).filter_by(name=name).one()

    def _on_checkout_completed(self, session):
        if session.get("mode") != "subscription":
            return
        sub = self._find_subscription(session.get("subscription"), session.get("client_reference_id"))
        if sub is None:
            return
        sub.plan_id = self._plan("pro").id
        sub.status = "active"
        sub.stripe_customer_id = session.get("customer")
        sub.stripe_subscription_id = session.get("subscription")

    def _on_subscription_updated(self, obj):
        sub = self._find_subscription(obj.get("id"), (obj.get("metadata") or {}).get("tenant_id"))
        new_status = STATUS_MAP.get(obj.get("status"))
        if sub is None or new_status is None:
            return
        if new_status == "canceled":
            return self._downgrade(sub)
        sub.stripe_subscription_id = obj["id"]
        sub.stripe_customer_id = obj.get("customer") or sub.stripe_customer_id
        sub.status = new_status
        sub.plan_id = self._plan("pro").id
        sub.current_period_start, sub.current_period_end = _period(obj)

    def _on_subscription_deleted(self, obj):
        sub = self._find_subscription(obj.get("id"), (obj.get("metadata") or {}).get("tenant_id"))
        if sub is not None:
            self._downgrade(sub)

    def _downgrade(self, sub):
        #Cancelled Pro subscriptions become free ones
        sub.plan_id = self._plan("free").id
        sub.status = "active"
        sub.current_period_start = sub.current_period_end = None