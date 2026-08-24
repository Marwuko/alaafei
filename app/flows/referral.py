"""The referral loop.

Message conventions for the demo (guided prompts come later; commands are
enough to prove the loop and are honest about being an MVP):

  Nurse:    REFER <patient name> | <danger sign> | <caregiver number>
  Facility: ARRIVED <referral id>
  Nurse:    CLOSE <referral id>

Everything else gets a gentle help message.
"""
from datetime import timedelta

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import (
    Facility,
    Household,
    MessageLog,
    Nurse,
    Referral,
    ReferralStatus,
    utcnow,
)
from app.ai.parse import parse_referral
from app.wa_client import (
    send_facility_template,
    send_referral_template,
    send_text,
    send_voice_note,
    send_voice_note_bilingual,
)

# sender -> parsed referral awaiting YES confirmation
_PENDING: dict[str, dict] = {}

HELP = (
    "Alaafei commands:\n"
    "REFER name | danger sign | caregiver number\n"
    "ARRIVED referral-id\n"
    "CLOSE referral-id\n"
    "PRIORITY - your ranked visit list\n"
    "JOIN your name - register yourself as a nurse\n"
    "\n"
    "Voice guidance (add a number, or leave blank to get it yourself):\n"
    "FEEDING - feeding for 6-23 months\n"
    "DANGER - danger signs in pregnancy\n"
    "CHILD - danger signs in a child\n"
    "ANC - antenatal visit reminder\n"
    "TRANSPORT - getting to the facility"
)


async def handle_inbound_text(sender: str, body: str) -> None:
    text = body.strip()
    upper = text.upper()

    if upper.startswith("REFER "):
        await _register_referral(sender, text[6:])
    elif upper.startswith("ARRIVED "):
        await _confirm_arrival(sender, text[8:])
    elif upper.startswith("CLOSE "):
        await _close_referral(sender, text[6:])
    elif upper.startswith("JOIN "):
        await _join_as_nurse(sender, text[5:])
    elif upper == "PRIORITY":
        await _send_priority_list(sender)
    elif upper in ("YES", "Y", "OK"):
        await _confirm_pending(sender)
    elif upper in ("NO", "N", "CANCEL"):
        _PENDING.pop(sender, None)
        await send_text(sender, "Cancelled. Nothing was registered.")
    elif upper.startswith("FEEDING"):
        await _send_advice(sender, text[7:], "feeding_6_23m", "feeding guidance")
    elif upper.startswith("DANGER"):
        await _send_advice(sender, text[6:], "danger_signs_pregnancy", "danger signs")
    elif upper.startswith("CHILD"):
        await _send_advice(sender, text[5:], "danger_signs_child", "child danger signs")
    elif upper.startswith("ANC"):
        await _send_advice(sender, text[3:], "anc_reminder", "ANC reminder")
    elif upper.startswith("TRANSPORT"):
        await _send_advice(sender, text[9:], "transport_reminder", "transport reminder")
    elif await _is_caregiver(sender):
        await _serve_caregiver(sender, text)
    else:
        parsed = await parse_referral(text)
        if parsed is None:
            await send_text(sender, HELP)
            return
        _PENDING[sender] = parsed
        num = parsed["caregiver_number"] or "none given"
        await send_text(
            sender,
            f"I understood:\n"
            f"Patient: {parsed['patient']}\n"
            f"Danger sign: {parsed['danger_sign']}\n"
            f"Caregiver: {num}\n\n"
            "Reply YES to register, NO to cancel.",
        )


async def _confirm_pending(sender: str) -> None:
    parsed = _PENDING.pop(sender, None)
    if parsed is None:
        await send_text(sender, HELP)
        return
    payload = f"{parsed['patient']} | {parsed['danger_sign']}"
    if parsed["caregiver_number"]:
        payload += f" | {parsed['caregiver_number']}"
    await _register_referral(sender, payload)


async def _join_as_nurse(sender: str, payload: str) -> None:
    """Self-registration so a new CHPS nurse can onboard from the phone."""
    parts = [p.strip() for p in payload.split("|")]
    name = parts[0]
    zone = parts[1] if len(parts) > 1 and parts[1] else "Demo Zone"
    if not name:
        await send_text(sender, "Format: JOIN your name")
        return
    async with SessionLocal() as session:
        existing = (
            await session.execute(select(Nurse).where(Nurse.wa_number == sender))
        ).scalar_one_or_none()
        if existing is not None:
            await send_text(
                sender,
                f"You are already registered as {existing.name} ({existing.chps_zone}).",
            )
            return
        session.add(Nurse(name=name, wa_number=sender, chps_zone=zone))
        await session.commit()
    await send_text(
        sender,
        f"Welcome {name}. You are registered for {zone}.\n"
        "Try: REFER Amina | heavy bleeding | caregiver number",
    )


async def _register_referral(sender: str, payload: str) -> None:
    parts = [p.strip() for p in payload.split("|")]
    if len(parts) < 2:
        await send_text(sender, "Format: REFER name | danger sign | caregiver number")
        return
    patient, danger = parts[0], parts[1]
    caregiver_number = parts[2] if len(parts) > 2 and parts[2] else None

    async with SessionLocal() as session:
        nurse = (
            await session.execute(select(Nurse).where(Nurse.wa_number == sender))
        ).scalar_one_or_none()
        if nurse is None:
            await send_text(sender, "This number is not registered as a nurse yet.")
            return

        facility = (await session.execute(select(Facility))).scalars().first()
        household = Household(
            caregiver_name=f"Caregiver of {patient}",
            caregiver_number=caregiver_number,
            community=nurse.chps_zone,
        )
        session.add(household)
        await session.flush()

        referral = Referral(
            nurse_id=nurse.id,
            facility_id=facility.id,
            household_id=household.id,
            patient_name=patient,
            danger_sign=danger,
        )
        session.add(referral)
        await session.commit()
        rid = referral.id
        facility_name = facility.name
        facility_number = facility.wa_number

    await send_text(
        sender,
        f"Referral #{rid} registered for {patient} ({danger}) -> {facility_name}. "
        "The caregiver will receive reminders. You will be alerted if they do not arrive.",
    )
    # Caregiver-facing messages: voice-first, text alongside.
    target = caregiver_number or sender  # no-phone household -> via nurse
    if caregiver_number:
        # Cold number: a template is the only thing Meta lets through.
        # Their reply opens the 24h window and the voice notes follow.
        await send_referral_template(caregiver_number, patient, facility_name)
    else:
        await send_voice_note_bilingual(target, clip="welcome")
        await send_voice_note_bilingual(target, clip="transport_reminder")
    ok = await send_text(
        facility_number,
        f"Incoming referral #{rid}: {patient} — {danger}. Reply ARRIVED {rid} when they arrive.",
    )
    if not ok:
        # Facility window closed -- template is the only way in.
        await send_facility_template(facility_number, patient, danger, rid)

    async with SessionLocal() as session:
        ref = await session.get(Referral, rid)
        ref.status = ReferralStatus.CAREGIVER_NOTIFIED
        ref.notified_at = utcnow()
        await session.commit()


async def _confirm_arrival(sender: str, payload: str) -> None:
    rid = _parse_id(payload)
    if rid is None:
        await send_text(sender, "Format: ARRIVED referral-id")
        return
    async with SessionLocal() as session:
        referral = await session.get(Referral, rid)
        if referral is None:
            await send_text(sender, f"No referral #{rid} found.")
            return
        referral.status = ReferralStatus.ARRIVED
        referral.arrived_at = utcnow()
        await session.commit()
        nurse_number = (await session.get(Nurse, referral.nurse_id)).wa_number
        household = await session.get(Household, referral.household_id)

    await send_text(sender, f"Referral #{rid} confirmed. Thank you.")
    await send_text(nurse_number, f"Good news: referral #{rid} ({referral.patient_name}) has arrived.")
    if household.caregiver_number:
        await send_voice_note_bilingual(household.caregiver_number, clip="arrival_thanks")


async def _close_referral(sender: str, payload: str) -> None:
    rid = _parse_id(payload)
    if rid is None:
        await send_text(sender, "Format: CLOSE referral-id")
        return
    async with SessionLocal() as session:
        referral = await session.get(Referral, rid)
        if referral is None:
            await send_text(sender, f"No referral #{rid} found.")
            return
        referral.status = ReferralStatus.CLOSED
        await session.commit()
    await send_text(sender, f"Referral #{rid} closed. Well done.")


async def _send_priority_list(sender: str) -> None:
    from app.prioritization.rules import compute_priorities, format_priority_message

    async with SessionLocal() as session:
        nurse = (
            await session.execute(select(Nurse).where(Nurse.wa_number == sender))
        ).scalar_one_or_none()
        if nurse is None:
            await send_text(sender, "This number is not registered as a nurse yet.")
            return
        items = await compute_priorities(session)
    await send_text(sender, format_priority_message(items))


async def escalate_overdue() -> None:
    """Runs on a schedule. Any referral notified > ESCALATION_HOURS ago and
    still unconfirmed gets escalated to its nurse — the moment that makes
    Alaafei different from a reminder app."""
    cutoff = utcnow() - timedelta(hours=settings.escalation_hours)
    async with SessionLocal() as session:
        overdue = (
            (
                await session.execute(
                    select(Referral).where(
                        Referral.status == ReferralStatus.CAREGIVER_NOTIFIED,
                        Referral.notified_at < cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
        for referral in overdue:
            referral.status = ReferralStatus.ESCALATED
            referral.escalated_at = utcnow()
            nurse = await session.get(Nurse, referral.nurse_id)
            household = await session.get(Household, referral.household_id)
            await session.commit()
            await send_text(
                nurse.wa_number,
                f"ALERT: referral {referral.id} for {referral.patient_name} with "
                f"{referral.danger_sign} has not been confirmed at the facility "
                f"after {settings.escalation_hours}h. Please follow up with "
                f"{household.caregiver_name} in {household.community}.",
            )
            if household.caregiver_number:
                await send_voice_note_bilingual(household.caregiver_number, clip="gentle_nudge")


def _parse_id(payload: str) -> int | None:
    try:
        return int(payload.strip().split()[0])
    except (ValueError, IndexError):
        return None


async def _is_caregiver(sender: str) -> bool:
    """True if this number is a caregiver on some household and not a nurse."""
    async with SessionLocal() as session:
        nurse = (
            await session.execute(select(Nurse).where(Nurse.wa_number == sender))
        ).scalar_one_or_none()
        if nurse is not None:
            return False
        hh = (
            await session.execute(
                select(Household).where(Household.caregiver_number == sender)
            )
        ).scalars().first()
        return hh is not None


async def _serve_caregiver(sender: str, text: str) -> None:
    """First message opens the 24h window and earns the voice notes.
    Anything after that is a question -- relay it to the facility and nurse."""
    async with SessionLocal() as session:
        seen = (
            await session.execute(
                select(MessageLog).where(MessageLog.from_number == sender)
            )
        ).scalars().all()
    if len(seen) <= 1:
        await send_text(
            sender,
            "Thank you. Here is a message from your nurse - it plays in "
            "Dagbani, then English.",
        )
        await send_voice_note_bilingual(sender, clip="welcome")
        await send_voice_note_bilingual(sender, clip="transport_reminder")
        await send_text(
            sender,
            "If you need anything, just send a message here and the health "
            "centre will see it.",
        )
        return
    await _relay_to_facility(sender, text)


async def _relay_to_facility(sender: str, text: str) -> None:
    """Pass a caregiver's message to the facility and the referring nurse."""
    async with SessionLocal() as session:
        household = (
            await session.execute(
                select(Household).where(Household.caregiver_number == sender)
            )
        ).scalars().first()
        referral = (
            await session.execute(
                select(Referral)
                .where(Referral.household_id == household.id)
                .order_by(Referral.id.desc())
            )
        ).scalars().first()
        if referral is None:
            await send_text(sender, "Thank you. Your nurse will be in touch.")
            return
        facility = await session.get(Facility, referral.facility_id)
        nurse = await session.get(Nurse, referral.nurse_id)

    note = (
        f"Message from the family of {referral.patient_name} "
        f"(referral #{referral.id}, {household.community}):\n\n"
        f'"{text}"\n\n'
        f"Reply to them on {sender}."
    )
    reached = await send_text(facility.wa_number, note)
    await send_text(nurse.wa_number, note)
    if reached:
        await send_text(sender, "Your message has been sent to the health centre.")
    else:
        await send_text(sender, "Your message has been sent to your nurse.")


def _normalise(num: str) -> str:
    n = "".join(ch for ch in num if ch.isdigit())
    if n.startswith("0"):
        n = "233" + n[1:]
    return n


async def _send_advice(sender: str, payload: str, clip: str, label: str) -> None:
    """Send a guidance voice note. No number -> send to the nurse herself so
    she can play it to a household that has no phone."""
    target = _normalise(payload)
    if not target:
        await send_voice_note_bilingual(sender, clip=clip)
        await send_text(sender, f"Sent you the {label} to play for the family.")
        return
    await send_text(
        target,
        f"A voice message from your nurse about {label}. "
        "Listen below - it plays in Dagbani, then English.",
    )
    await send_voice_note_bilingual(target, clip=clip)
    await send_text(
        sender,
        f"Sent the {label} to {target}. If they have not messaged Alaafei "
        "recently it may not reach them - play it to them yourself instead.",
    )
