from contextlib import asynccontextmanager
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from app.db import engine, ensure_raw_tables
from app.flows.referral import escalate_overdue, follow_up_escalated
from app.media import refresh_all as refresh_media
from app.models import Base
from app.auth import router as auth_router
from app.dashboard import router as dashboard_router
from app.webhooks import router as webhook_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_raw_tables()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(escalate_overdue, "interval", minutes=15)
    scheduler.add_job(follow_up_escalated, "interval", minutes=15)
    # Voice clips: upload anything missing or ageing, now and then daily,
    # so a media id never reaches the age at which Meta drops it.
    scheduler.add_job(refresh_media, "interval", hours=24, next_run_time=datetime.now())
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
