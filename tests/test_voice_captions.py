"""A voice note never arrives bare. Every clip is preceded by one line of
text to the same number saying which referral it is about and why it came,
and a clip is not fired at a number whose 24h window is shut."""
import os

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_alaafei.db"

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy import text as sql

from app import thread
from app.db import SessionLocal, engine, ensure_raw_tables
from app.flows import referral as flow
from app.models import Base, Facility, Nurse, Referral, utcnow
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
def events(monkeypatch):
    """Everything outbound, in order: ("text", to, body) or ("clip", to, name)."""
    log: list[tuple[str, str, str]] = []

    async def send_text(to, body):
        log.append(("text", to, body))
        return True

    async def send_buttons(to, body, buttons):
        log.append(("text", to, body))
        return True

    async def send_voice(to, clip):
        log.append(("clip", to, clip))

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(flow, "send_text", send_text)
    monkeypatch.setattr(thread, "send_text", send_text)
    monkeypatch.setattr(flow, "send_buttons", send_buttons)
    monkeypatch.setattr(flow, "send_voice_note_bilingual", send_voice)
    monkeypatch.setattr(flow, "send_referral_template", noop)
    monkeypatch.setattr(flow, "send_facility_template", noop)
    return log


def clips(log):
    return [(to, name) for kind, to, name in log if kind == "clip"]


def assert_every_clip_captioned(log):
    """Walking back from each clip through that number's messages, a text
    comes before any gap: one caption may cover a run of clips (the welcome
    and transport pair), but no clip is the first thing a number hears."""
    for i, (kind, to, name) in enumerate(log):
        if kind != "clip":
            continue
        before = [e for e in log[:i] if e[1] == to]
        assert before, f"{name} to {to} arrived with nothing before it"
        assert any(e[0] == "text" for e in before), f"{name} to {to} was not captioned"


async def _rid() -> int:
    async with SessionLocal() as s:
        return (await s.execute(select(Referral))).scalar_one().id


@pytest.mark.asyncio
async def test_first_reply_names_patient_and_facility(events):
    await flow.handle_inbound_text(NURSE, f"REFER Fuseina | heavy bleeding | {CAREGIVER}")
    rid = await _rid()
    await flow.handle_inbound_text(CAREGIVER, "hi")

    assert clips(events) == [(CAREGIVER, "welcome"), (CAREGIVER, "transport_reminder")]
    assert_every_clip_captioned(events)
    caption = next(b for k, to, b in events if k == "text" and to == CAREGIVER)
    assert "Fuseina" in caption and "Savelugu HC" in caption and f"#{rid}" in caption
    assert "Dagbani" in caption


@pytest.mark.asyncio
async def test_play_again_is_captioned(events):
    await flow.handle_inbound_text(NURSE, f"REFER Fuseina | heavy bleeding | {CAREGIVER}")
    await flow.handle_inbound_text(CAREGIVER, "hi")
    events.clear()

    await flow.handle_inbound_text(CAREGIVER, "CARE_AGAIN")

    assert clips(events) == [(CAREGIVER, "welcome"), (CAREGIVER, "transport_reminder")]
    assert_every_clip_captioned(events)
    assert "Savelugu HC" in events[0][2]


@pytest.mark.asyncio
async def test_no_phone_household_clips_to_nurse_say_why(events):
    await flow.handle_inbound_text(NURSE, "REFER Fuseina | heavy bleeding | ")
    rid = await _rid()

    assert clips(events) == [(NURSE, "welcome"), (NURSE, "transport_reminder")]
    assert_every_clip_captioned(events)
    caption = [b for k, to, b in events if k == "text" and to == NURSE][-1]
    assert f"#{rid}" in caption and "No number" in caption and "Fuseina" in caption


@pytest.mark.asyncio
async def test_arrival_thanks_is_captioned(events):
    await flow.handle_inbound_text(NURSE, f"REFER Fuseina | heavy bleeding | {CAREGIVER}")
    rid = await _rid()
    events.clear()

    await flow.handle_inbound_text(FACILITY, f"ARRIVED {rid}")

    assert clips(events) == [(CAREGIVER, "arrival_thanks")]
    assert_every_clip_captioned(events)
    caption = [b for k, to, b in events if k == "text" and to == CAREGIVER][-1]
    assert "Fuseina" in caption and "confirmed at Savelugu HC" in caption


@pytest.mark.asyncio
async def test_arrival_thanks_not_fired_into_a_shut_window(events):
    await flow.handle_inbound_text(NURSE, f"REFER Fuseina | heavy bleeding | {CAREGIVER}")
    rid = await _rid()
    async with SessionLocal() as s:
        await s.execute(sql("DELETE FROM wa_windows WHERE wa_number = :n"), {"n": CAREGIVER})
        await s.commit()
    events.clear()

    await flow.handle_inbound_text(FACILITY, f"ARRIVED {rid}")

    assert clips(events) == []
    assert all(to != CAREGIVER for _, to, _ in events)


@pytest.mark.asyncio
async def test_escalation_nudge_names_everyone_overdue(events):
    await flow.handle_inbound_text(NURSE, f"REFER Fuseina | heavy bleeding | {CAREGIVER}")
    await flow.handle_inbound_text(NURSE, f"REFER Amina | fever | {CAREGIVER}")
    async with SessionLocal() as s:
        for r in (await s.execute(select(Referral))).scalars().all():
            r.notified_at = utcnow() - timedelta(hours=72)
        await s.commit()
    events.clear()

    await flow.escalate_overdue()

    assert clips(events) == [(CAREGIVER, "gentle_nudge")]
    assert_every_clip_captioned(events)
    caption = [b for k, to, b in events if k == "text" and to == CAREGIVER][-1]
    assert caption.startswith("Fuseina and Amina have not yet been confirmed at Savelugu HC.")
    assert "asks you to go" in caption


@pytest.mark.asyncio
async def test_escalation_nudge_skips_a_shut_window(events):
    await flow.handle_inbound_text(NURSE, f"REFER Fuseina | heavy bleeding | {CAREGIVER}")
    async with SessionLocal() as s:
        for r in (await s.execute(select(Referral))).scalars().all():
            r.notified_at = utcnow() - timedelta(hours=72)
        await s.commit()
        await s.execute(sql("DELETE FROM wa_windows WHERE wa_number = :n"), {"n": CAREGIVER})
        await s.commit()
    events.clear()

    await flow.escalate_overdue()

    assert clips(events) == []
    # The nurse is still told.
    assert any(to == NURSE and "Not confirmed" in b for k, to, b in events)


@pytest.mark.asyncio
async def test_guidance_clips_are_captioned_both_ways(events):
    await flow.handle_inbound_text(NURSE, "TRANSPORT")
    await flow.handle_inbound_text(NURSE, "FEEDING 0242000000")

    assert clips(events) == [
        (NURSE, "transport_reminder"), ("233242000000", "feeding_6_23m"),
    ]
    assert_every_clip_captioned(events)
