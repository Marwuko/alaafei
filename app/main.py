from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from app.db import engine
from app.flows.referral import escalate_overdue
from app.models import Base
from app.auth import router as auth_router
from app.dashboard import router as dashboard_router
from app.webhooks import router as webhook_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(escalate_overdue, "interval", minutes=15)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Alaafei", lifespan=lifespan)
app.include_router(webhook_router)
app.include_router(auth_router)
app.include_router(dashboard_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "alaafei"}
