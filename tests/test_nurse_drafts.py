"""The AI-drafted reply buttons: a household writes in, the nurse gets the
message with up to two drafted answers as tappable buttons, a tap sends the
draft to the household in one thread.

The model and Meta are both stubbed. What this proves is everything between
them: the suggestion shape, the Meta limits on the payload, the button id
round-tripping back through the router as a REPLY command, and the household
getting the words the nurse tapped."""
import os

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_alaafei.db"

import pytest
from sqlalchemy import select
from sqlalchemy import text as sql

from app import thread
from app.ai.caregiver import _clean_suggestions
from app.db import SessionLocal, engine, ensure_raw_tables
from app.flows import referral as flow
from app.models import Base, Facility, Nurse, Referral
from app.wa_window import mark_inbound

NURSE = "233200000001"
FACILITY = "233200000002"
CAREGIVER = "233200000003"

# What the model actually returns for "we have no money for transport":
# one label over the 20-character button limit on purpose.
MODEL_PLAN = {
    "reply": "I hear you. A health worker will be told about the transport "
             "cost so they can help you plan.",
    "notify": True,
    "summary": "cannot afford transport fare",
    "urgent": False,
    "suggestions": [
        {"label": "Ask what they can pay",
         "text": "How much can you manage for transport today? Tell me and "
                 "we will see what is possible."},
        {"label": "Offer to arrange transport",
         "text": "Stay where you are. I will call the driver at the junction "
                 "and send him to you."},
    ],
}


class Outbox:
    def __init__(self):
        self.texts: list[tuple[str, str]] = []
        self.buttons: list[tuple[str, str, list[tuple[str, str]]]] = []
        self.clips: list[tuple[str, str]] = []
        self.templates: list[tuple[str, str]] = []

    def to(self, number):
        return [t for t in self.texts if t[0] == number]


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


@pytest.fixture
def outbox(monkeypatch):
    box = Outbox()

    async def send_text(to, body):
        box.texts.append((to, body))
        return True

    async def send_buttons(to, body, buttons):
        box.buttons.append((to, body, list(buttons)))
        return True

    async def send_voice(to, clip):
        box.clips.append((to, clip))

    async def facility_template(to, patient, danger, rid):
        box.templates.append((to, "facility"))

    async def nurse_template(to, patient, rid, summary):
        box.templates.append((to, "nurse"))

    async def understand(text):
        plan = dict(MODEL_PLAN)
        plan["suggestions"] = _clean_suggestions(MODEL_PLAN["suggestions"])
        return plan

    monkeypatch.setattr(flow, "send_text", send_text)
    monkeypatch.setattr(thread, "send_text", send_text)
    monkeypatch.setattr(flow, "send_buttons", send_buttons)
    monkeypatch.setattr(flow, "send_voice_note_bilingual", send_voice)
    monkeypatch.setattr(flow, "send_facility_template", facility_template)
    monkeypatch.setattr(flow, "send_nurse_template", nurse_template)
    monkeypatch.setattr(flow, "understand_caregiver", understand)
    return box


async def _referral_with_open_windows(outbox) -> int:
    """Register a referral and put every phone inside its 24h window, which
    is what the morning 'hi' ritual does on the demo phones."""
    for n in (NURSE, FACILITY, CAREGIVER):
        await mark_inbound(n)
    await flow.handle_inbound_text(NURSE, f"REFER Fuseina | heavy bleeding | {CAREGIVER}")
    async with SessionLocal() as s:
        rid = (await s.execute(select(Referral))).scalar_one().id
    # First household reply goes down the voice-note path, not to the model.
    await flow.handle_inbound_text(CAREGIVER, "hello")
    assert (CAREGIVER, "welcome") in outbox.clips
    outbox.texts.clear(); outbox.buttons.clear(); outbox.clips.clear()
    return rid


def _nurse_buttons(outbox):
    return [b for b in outbox.buttons if b[0] == NURSE]


@pytest.mark.asyncio
async def test_second_message_gives_nurse_tappable_drafts(outbox):
    rid = await _referral_with_open_windows(outbox)

    await flow.handle_inbound_text(CAREGIVER, "We have no money for transport")

    # The household got the model's reply, nothing clinical, nothing else.
    assert outbox.to(CAREGIVER) == [(CAREGIVER, MODEL_PLAN["reply"])]
    # The facility got plain text (it has no reply path), inside its window.
    assert any(f"referral #{rid}" in body for to, body in outbox.to(FACILITY))
    assert outbox.templates == []

    cards = _nurse_buttons(outbox)
    assert len(cards) == 1, "nurse should get exactly one button card"
    _, body, buttons = cards[0]

    # Meta's interactive-reply limits, checked where the payload is built.
    assert 1 <= len(buttons) <= 3
    assert len(body) <= 1024
    ids = [bid for bid, _ in buttons]
    assert len(set(ids)) == len(ids)
    for bid, label in buttons:
        assert bid.startswith(f"REPLY {rid} ")
        assert len(bid) <= 256
        assert 0 < len(label) <= 20
        assert label == label.strip()
    # The long label was cut at a word, not mid-word.
    assert buttons[1][1] == "Offer to arrange"
    assert buttons[0][1] == "Ask what they can"
    # She can read the exact words before tapping.
    for s_ in MODEL_PLAN["suggestions"]:
        assert s_["text"] in body
    assert "cannot afford transport fare" in body
    assert f"REPLY {rid}" in body


@pytest.mark.asyncio
async def test_tap_sends_the_draft_to_the_household(outbox):
    rid = await _referral_with_open_windows(outbox)
    await flow.handle_inbound_text(CAREGIVER, "We have no money for transport")
    _, _, buttons = _nurse_buttons(outbox)[0]
    tapped_id, _ = buttons[0]
    outbox.texts.clear()

    # A button tap arrives through the webhook as its id, verbatim.
    await flow.handle_inbound_text(NURSE, tapped_id)

    draft = MODEL_PLAN["suggestions"][0]["text"]
    assert outbox.to(CAREGIVER) == [(CAREGIVER, f"Abiba (health worker): {draft}")]
    assert any("Sent on referral" in body for _, body in outbox.to(NURSE))
    assert await thread.nurse_has_spoken(rid)

    # The whole exchange is on the referral's thread for the dashboard.
    async with SessionLocal() as s:
        rows = (await s.execute(
            sql("SELECT direction, body, delivered FROM referral_messages "
                "WHERE referral_id = :r ORDER BY id"), {"r": rid}
        )).fetchall()
    assert [(d, bool(ok)) for d, _, ok in rows] == [("from_family", True), ("to_family", True)]
    assert rows[1][1].endswith(draft)


@pytest.mark.asyncio
async def test_nurse_window_shut_means_template_not_buttons(outbox):
    rid = await _referral_with_open_windows(outbox)
    async with SessionLocal() as s:
        await s.execute(sql("DELETE FROM wa_windows WHERE wa_number = :n"), {"n": NURSE})
        await s.commit()

    await flow.handle_inbound_text(CAREGIVER, "We have no money for transport")

    assert _nurse_buttons(outbox) == []
    assert (NURSE, "nurse") in outbox.templates


@pytest.mark.asyncio
async def test_household_answering_ok_is_not_a_nurse_command(outbox):
    """After the nurse's draft lands, the household answers 'ok' or 'yes'.
    That must stay inside the conversation. It used to hit the nurse's
    YES-to-register branch and send the household the command menu."""
    rid = await _referral_with_open_windows(outbox)
    await flow.handle_inbound_text(CAREGIVER, "We have no money for transport")
    tapped_id = _nurse_buttons(outbox)[0][2][0][0]
    await flow.handle_inbound_text(NURSE, tapped_id)
    outbox.texts.clear(); outbox.buttons.clear()

    for word in ("ok", "Yes", "no"):
        await flow.handle_inbound_text(CAREGIVER, word)

    for _, body in outbox.to(CAREGIVER):
        assert "Alaafei commands" not in body
        assert "Cancelled" not in body
    # On a live thread her every word reaches the nurse.
    assert len(_nurse_buttons(outbox)) + len(outbox.to(NURSE)) >= 3


@pytest.mark.asyncio
async def test_household_message_starting_with_a_keyword_still_relays(outbox):
    """'Transport is the problem' is the sentence the product exists for.
    It used to match the nurse's TRANSPORT guidance command instead."""
    rid = await _referral_with_open_windows(outbox)

    await flow.handle_inbound_text(CAREGIVER, "Transport is the problem, no car here")
    await flow.handle_inbound_text(CAREGIVER, "Child is still crying")

    assert outbox.clips == []
    assert all("Sent you the" not in body for _, body in outbox.to(CAREGIVER))
    assert len(_nurse_buttons(outbox)) == 2
