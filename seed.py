from database import SessionLocal
from models import Plan, Tenant, Subscription


# Did this so it would be easy to change limits
PLANS = [
    {"name": "free", "api_call_limit": 1_000, "ai_token_limit": 100_000, "price_cents": 0},
    {"name": "pro", "api_call_limit": 10_000, "ai_token_limit": 1_000_000, "price_cents": 2900},
]

def seed():
    db = SessionLocal()
    try:
        # Only insert a plan if it doesn't exist yet
        # Means running the script multiple times doesn't create duplicates
        for p in PLANS:
            if not db.query(Plan).filter_by(name=p["name"]).first():
                db.add(Plan(**p))
        db.commit()

        free = db.query(Plan).filter_by(name="free").one()
        tenant = db.query(Tenant).filter_by(name="demo-tenant").first()
        if not tenant:
            tenant = Tenant(name="demo-tenant")
            db.add(tenant)
            db.commit()
            db.add(Subscription(tenant_id=tenant.id, plan_id=free.id, status="active"))
            db.commit()  # Commit first so tenant.id is available
        print(f"Seeded. Demo tenant id: {tenant.id}")
    finally:
        db.close()

if __name__ == "__main__":
    seed()