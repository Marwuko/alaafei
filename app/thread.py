"""Every exchange between a nurse and a family belongs to a referral, not to
somebody's personal call log. This keeps both sides in one WhatsApp thread and
writes the whole conversation down against the referral it is about.

If the family's 24-hour window is shut we cannot push free text at them, so the
message is held and sent the moment they next write in."""

from sqlalchemy import text as sql

from app.db import SessionLocal
from app.models import utcnow
from app.wa_client import send_text
from app.wa_window import window_open


async def log(referral_id: int, direction: str, from_number: str,
              body: str, delivered: bool = True) -> None:
    async with SessionLocal() as session:
        await session.execute(
            sql(
                "INSERT INTO referral_messages "
                "(referral_id, direction, from_number, body, created_at, delivered) "
                "VALUES (:r, :d, :f, :b, :t, :ok)"
            ),
            {"r": referral_id, "d": direction, "f": from_number, "b": body,
             "t": utcnow(), "ok": 1 if delivered else 0},
        )
        await session.commit()


async def deliver_or_hold(referral_id: int, to_number: str, from_number: str,
                          body: str) -> bool:
    """True if it went out now, False if it is waiting for them to write in."""
    if await window_open(to_number):
        await send_text(to_number, body)
        await log(referral_id, "to_family", from_number, body, delivered=True)
        return True
    await log(referral_id, "to_family", from_number, body, delivered=False)
    print(f"[thread] held reply for {to_number} on #{referral_id}", flush=True)
    return False


async def flush(to_number: str) -> None:
    """Called when anyone writes in. Sends anything that was waiting on them."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                sql(
                    "SELECT rm.id, rm.body FROM referral_messages rm "
                    "JOIN referrals r ON r.id = rm.referral_id "
                    "JOIN households h ON h.id = r.household_id "
                    "WHERE rm.delivered = 0 AND h.caregiver_number = :n "
                    "ORDER BY rm.id ASC"
                ),
                {"n": to_number},
            )
        ).fetchall()
    for mid, body in rows:
        await send_text(to_number, body)
        async with SessionLocal() as session:
            await session.execute(
                sql("UPDATE referral_messages SET delivered = 1 WHERE id = :i"),
                {"i": mid},
            )
            await session.commit()
    if rows:
        print(f"[thread] flushed {len(rows)} held messages to {to_number}", flush=True)


async def nurse_has_spoken(referral_id: int) -> bool:
    """Once a nurse has written to a family about a referral, that thread is
    live. Everything the family says back belongs to her, not just the parts
    a classifier decides are urgent."""
    async with SessionLocal() as session:
        row = (
            await session.execute(
                sql(
                    "SELECT 1 FROM referral_messages "
                    "WHERE referral_id = :r AND direction = 'to_family' LIMIT 1"
                ),
                {"r": referral_id},
            )
        ).first()
    return row is not None
