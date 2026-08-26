"""Meta only allows free-form text within 24 hours of the person's last
inbound message. The API does not tell us at send time -- a doomed send still
returns 200 and the 131047 arrives later on the status webhook. So we track
inbound timestamps ourselves and decide before sending."""

from datetime import timedelta

from sqlalchemy import text

from app.db import SessionLocal
from app.models import utcnow

WINDOW = timedelta(hours=24)


async def mark_inbound(wa_number: str) -> None:
    """Called for every message a person sends us. Opens their window."""
    async with SessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO wa_windows (wa_number, last_inbound_at) "
                "VALUES (:n, :t) "
                "ON CONFLICT(wa_number) DO UPDATE SET last_inbound_at = :t"
            ),
            {"n": wa_number, "t": utcnow()},
        )
        await session.commit()


async def window_open(wa_number: str) -> bool:
    """True when we may still send free-form text to this number."""
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT last_inbound_at FROM wa_windows WHERE wa_number = :n"
                ),
                {"n": wa_number},
            )
        ).first()
    if row is None or row[0] is None:
        return False
    ts = row[0]
    if isinstance(ts, str):
        from datetime import datetime

        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return False
    now = utcnow()
    if ts.tzinfo is None and now.tzinfo is not None:
        ts = ts.replace(tzinfo=now.tzinfo)
    elif ts.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=ts.tzinfo)
    return (now - ts) < WINDOW


async def remember_template(wa_number: str, referral_id: int) -> None:
    """A template carries no state back to us -- its Quick Reply button sends
    a fixed payload. Record what the last template to this number was about so
    the tap can be resolved."""
    async with SessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO template_context (wa_number, referral_id, sent_at) "
                "VALUES (:n, :r, :t) "
                "ON CONFLICT(wa_number) DO UPDATE SET referral_id = :r, sent_at = :t"
            ),
            {"n": wa_number, "r": referral_id, "t": utcnow()},
        )
        await session.commit()


async def last_template_referral(wa_number: str) -> int | None:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT referral_id FROM template_context WHERE wa_number = :n"
                ),
                {"n": wa_number},
            )
        ).first()
    return row[0] if row else None
