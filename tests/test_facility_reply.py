"""The receiving facility can answer the household on its own referrals.
It is signed with the facility's name, the nurse is told, and the
exchange is on the same thread as everything else."""
import os

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_alaafei.db"

import pytest
from sqlalchemy import select
from sqlalchemy import text as sql

from app import thread
from app.db import SessionLocal, engine, ensure_raw_tables
from app.flows import referral as flow
from app.models import Base, Facility, FacilityZone, Nurse, Referral
from app.wa_window import mark_inbound

NURSE = "233200000001"
FACILITY = "233200000002"
CAREGIVER = "233200000003"
OTHER_FACILITY = "233200000009"


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
            Facility(name="Nanton HC", wa_number=OTHER_FACILITY, district="Nanton"),
        ])
        await s.commit()
    flow._PENDING.clear()
    flow._AWAITING_VOICE.clear()
    for n in (NURSE, FACILITY, CAREGIVER, OTHER_FACILITY):
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


def to(log, number):
    return [(k, b) for k, n, b in log if n == number]


async def _referral(outbox) -> int:
    await flow.handle_inbound_text(NURSE, f"REFER Fuseina | heavy bleeding | {CAREGIVER}")
    async with SessionLocal() as s:
        rid = (await s.execute(select(Referral))).scalar_one().id
    outbox.clear()
    return rid


@pytest.mark.asyncio
async def test_facility_reply_reaches_household_and_nurse(outbox):
    rid = await _referral(outbox)

    await flow.handle_inbound_text(FACILITY, f"REPLY {rid} We are open until 6, come to the maternity side")

    assert to(outbox, CAREGIVER) == [
        ("text", "Savelugu HC: We are open until 6, come to the maternity side")
    ]
    assert to(outbox, FACILITY) == [("text", f"Sent on referral {rid} for Fuseina.")]
    nurse_copy = to(outbox, NURSE)
    assert len(nurse_copy) == 1 and nurse_copy[0][0] == "text"
    assert "Savelugu HC wrote to the number on referral" in nurse_copy[0][1]
    assert "come to the maternity side" in nurse_copy[0][1]

    async with SessionLocal() as s:
        rows = (await s.execute(
            sql("SELECT direction, from_number, body, delivered FROM referral_messages "
                "WHERE referral_id = :r ORDER BY id"), {"r": rid}
        )).fetchall()
    assert [(r[0], r[1], bool(r[3])) for r in rows] == [("to_family", FACILITY, True)]
    assert await thread.nurse_has_spoken(rid)


@pytest.mark.asyncio
async def test_nurse_copy_uses_template_when_her_window_is_shut(outbox):
    rid = await _referral(outbox)
    async with SessionLocal() as s:
        await s.execute(sql("DELETE FROM wa_windows WHERE wa_number = :n"), {"n": NURSE})
        await s.commit()

    await flow.handle_inbound_text(FACILITY, f"REPLY {rid} Come tomorrow morning")

    assert to(outbox, NURSE) == [("template", "Savelugu HC replied: Come tomorrow morning")]


@pytest.mark.asyncio
async def test_facility_reply_is_held_when_household_window_is_shut(outbox):
    rid = await _referral(outbox)
    async with SessionLocal() as s:
        await s.execute(sql("DELETE FROM wa_windows WHERE wa_number = :n"), {"n": CAREGIVER})
        await s.commit()

    await flow.handle_inbound_text(FACILITY, f"REPLY {rid} Come tomorrow morning")

    assert to(outbox, CAREGIVER) == []
    assert any("saved and goes out" in b for _, b in to(outbox, FACILITY))
    assert any("Held until they write in" in b for _, b in to(outbox, NURSE))
    outbox.clear()

    # They write in: the held message goes out before anything else.
    await mark_inbound(CAREGIVER)
    await thread.flush(CAREGIVER)
    assert to(outbox, CAREGIVER) == [("text", "Savelugu HC: Come tomorrow morning")]


@pytest.mark.asyncio
async def test_another_facility_cannot_answer(outbox):
    rid = await _referral(outbox)

    await flow.handle_inbound_text(OTHER_FACILITY, f"REPLY {rid} Come to us instead")

    assert to(outbox, CAREGIVER) == []
    assert to(outbox, NURSE) == []
    assert to(outbox, OTHER_FACILITY) == [
        ("text", f"Only the nurse who made referral {rid} or Savelugu HC can answer on it.")
    ]


@pytest.mark.asyncio
async def test_household_answer_after_facility_reply_reaches_both(outbox, monkeypatch):
    """The desk asked a question; the household's answer, even a plain
    'ok', must get back to the desk and the nurse."""
    rid = await _referral(outbox)
    await flow.handle_inbound_text(FACILITY, f"REPLY {rid} Can you come before noon?")
    outbox.clear()

    async def understand(text):
        return {"reply": "Thank you.", "notify": False, "summary": "",
                "urgent": False, "suggestions": []}
    monkeypatch.setattr(flow, "understand_caregiver", understand)

    await flow.handle_inbound_text(CAREGIVER, "ok we will come")

    assert any("ok we will come" in b for _, b in to(outbox, FACILITY))
    assert any("ok we will come" in b for _, b in to(outbox, NURSE))


@pytest.mark.asyncio
async def test_desk_typing_bare_number_and_text_is_offered_as_a_reply(outbox):
    rid = await _referral(outbox)
    await flow.handle_inbound_text(FACILITY, f"REPLY {rid} Come before noon")
    outbox.clear()

    await flow.handle_inbound_text(FACILITY, f"{rid} bring her card")

    taps = [b for k, n, b in outbox if k == "taps" and n == FACILITY]
    assert taps == [[(f"REPLY {rid} bring her card", "Send to family"), ("CANCEL", "No")]]
    # Not the arrivals list.
    assert not any("Confirm arrival" in b for k, n, b in outbox if k == "text")
