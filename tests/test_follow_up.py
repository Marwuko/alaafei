"""An escalated referral is followed up, not alerted once and forgotten:
a check-in a day later, two more spaced three days apart with different
words, then a last one that says it is the last. Then silence; the
referral stays on the dashboard until the nurse closes it."""
import os

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_alaafei.db"

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy import text as sql

from app import thread
from app.db import SessionLocal, engine, ensure_raw_tables
from app.flows import referral as flow
from app.models import Base, Facility, Nurse, Referral, ReferralStatus, utcnow
from app.wa_window import mark_inbound

NURSE = "233200000001"
FACILITY = "233200000002"
CAREGIVER = "233200000003"


@pytest.fixture(autouse=True)
async def _setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        for t in ("wa_windows", "template_context", "referral_messages"):
            await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {t}")
        await conn.run_sync(Base.metadata.create_all)
    await ensure_raw_tables()
    async with SessionLocal() as s:
        s.add_all([
            Nurse(name="Abiba", wa_number=NURSE, chps_zone="Kpalsogu CHPS"),
            Facility(name="Savelugu HC", wa_number=FACILITY, district="Savelugu"),
        ])
        await s.commit()
    flow._PENDING.clear()
    flow._AWAITING_VOICE.clear()
    for n in (NURSE, FACILITY, CAREGIVER):
        await mark_inbound(n)


@pytest.fixture
def outbox(monkeypatch):
    log: list[tuple[str, str, str]] = []

    async def send_text(to, body):
        log.append(("text", to, body)); return True

    async def send_buttons(to, body, buttons):
        log.append(("buttons", to, body)); log.append(("taps", to, buttons)); return True

    async def nurse_template(to, patient, rid, summary):
        log.append(("template", to, summary))

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(flow, "send_text", send_text)
    monkeypatch.setattr(thread, "send_text", send_text)
    monkeypatch.setattr(flow, "send_buttons", send_buttons)
    monkeypatch.setattr(flow, "send_nurse_template", nurse_template)
    monkeypatch.setattr(flow, "send_voice_note_bilingual", noop)
    monkeypatch.setattr(flow, "send_referral_template", noop)
    monkeypatch.setattr(flow, "send_facility_template", noop)
    return log


def nurse_msgs(log):
    return [(k, b) for k, to, b in log if to == NURSE and k != "taps"]


async def _escalated(outbox, *names) -> list[int]:
    for name in names:
        await flow.handle_inbound_text(NURSE, f"REFER {name} | heavy bleeding | {CAREGIVER}")
    async with SessionLocal() as s:
        refs = (await s.execute(select(Referral))).scalars().all()
        for r in refs:
            r.notified_at = utcnow() - timedelta(hours=72)
        await s.commit()
        ids = [r.id for r in refs]
    await flow.escalate_overdue()
    outbox.clear()
    return ids


async def _age(hours: float) -> None:
    """Pretend time passed: push the alert and last reminder back."""
    async with SessionLocal() as s:
        for r in (await s.execute(select(Referral))).scalars().all():
            if r.escalated_at:
                r.escalated_at -= timedelta(hours=hours)
            if r.reminded_at:
                r.reminded_at -= timedelta(hours=hours)
        await s.commit()


@pytest.mark.asyncio
async def test_nothing_until_a_day_after_the_alert(outbox):
    await _escalated(outbox, "Fuseina")
    await flow.follow_up_escalated()
    assert nurse_msgs(outbox) == []
    await _age(23)
    await flow.follow_up_escalated()
    assert nurse_msgs(outbox) == []


@pytest.mark.asyncio
async def test_three_follow_ups_with_different_words_then_silence(outbox):
    (rid,) = await _escalated(outbox, "Fuseina")
    bodies = []
    for hours in (24, 72, 72, 72, 72):
        await _age(hours)
        await flow.follow_up_escalated()
        await flow.follow_up_escalated()  # a second pass in the same window
        bodies.append([b for _, b in nurse_msgs(outbox)])
        outbox.clear()

    assert [len(b) for b in bodies] == [1, 1, 1, 0, 0]
    first, second, last = bodies[0][0], bodies[1][0], bodies[2][0]
    assert first.startswith("Checking in")
    assert "stopping them" in second
    assert last.startswith("Last reminder")
    for body in (first, second, last):
        assert f"{rid}. Fuseina" in body
        assert "CLOSE" in body
    assert "1 day since the alert" in first
    assert "4 days since the alert" in second
    async with SessionLocal() as s:
        ref = await s.get(Referral, rid)
        assert ref.reminder_count == 3
        assert ref.status == ReferralStatus.ESCALATED


@pytest.mark.asyncio
async def test_single_referral_offers_a_close_button(outbox):
    (rid,) = await _escalated(outbox, "Fuseina")
    await _age(24)
    await flow.follow_up_escalated()

    taps = [b for k, to, b in outbox if k == "taps"]
    assert taps == [[(f"CLOSE {rid}", "Resolved, close it"), ("PRIORITY", "My visit list")]]
    outbox.clear()

    await flow.handle_inbound_text(NURSE, f"CLOSE {rid}")
    await _age(72)
    await flow.follow_up_escalated()
    assert [b for _, b in nurse_msgs(outbox) if "Checking" in b or "Still" in b] == []


@pytest.mark.asyncio
async def test_two_referrals_one_message(outbox):
    a, b = await _escalated(outbox, "Fuseina", "Amina")
    await _age(24)
    await flow.follow_up_escalated()

    msgs = nurse_msgs(outbox)
    assert len(msgs) == 1
    body = msgs[0][1]
    assert "these referrals" in body
    assert f"{a}. Fuseina" in body and f"{b}. Amina" in body
    assert [b for k, to, b in outbox if k == "taps"] == []


@pytest.mark.asyncio
async def test_window_shut_falls_back_to_the_nurse_template(outbox):
    (rid,) = await _escalated(outbox, "Fuseina")
    async with SessionLocal() as s:
        await s.execute(sql("DELETE FROM wa_windows WHERE wa_number = :n"), {"n": NURSE})
        await s.commit()
    await _age(24)
    await flow.follow_up_escalated()

    assert nurse_msgs(outbox) == [("template", "Checking in: still not confirmed at the facility.")]


@pytest.mark.asyncio
async def test_first_alert_also_respects_the_window(outbox):
    await flow.handle_inbound_text(NURSE, f"REFER Fuseina | heavy bleeding | {CAREGIVER}")
    async with SessionLocal() as s:
        for r in (await s.execute(select(Referral))).scalars().all():
            r.notified_at = utcnow() - timedelta(hours=72)
        await s.execute(sql("DELETE FROM wa_windows WHERE wa_number = :n"), {"n": NURSE})
        await s.commit()
    outbox.clear()

    await flow.escalate_overdue()

    kinds = [k for k, _ in nurse_msgs(outbox)]
    assert kinds == ["template"]


@pytest.mark.asyncio
async def test_live_database_gains_the_follow_up_columns():
    """The live SQLite file predates reminded_at/reminder_count. Startup
    adds them; a second startup leaves them alone."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.exec_driver_sql(
            "CREATE TABLE referrals (id INTEGER PRIMARY KEY, nurse_id INTEGER, "
            "facility_id INTEGER, household_id INTEGER, patient_name TEXT, "
            "danger_sign TEXT, status TEXT, created_at TIMESTAMP, "
            "notified_at TIMESTAMP, arrived_at TIMESTAMP, escalated_at TIMESTAMP)"
        )
    await ensure_raw_tables()
    await ensure_raw_tables()
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.exec_driver_sql("PRAGMA table_info(referrals)")).fetchall()}
    assert {"reminded_at", "reminder_count"} <= cols
