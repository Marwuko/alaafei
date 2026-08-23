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
from app.wa_client import send_text, send_voice_note

HELP = (
    "Alaafei commands:\n"
    "REFER name | danger sign | caregiver number\n"
    "ARRIVED referral-id\n"
    "CLOSE referral-id\n"
    "PRIORITY - your ranked visit list"
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
    elif upper == "PRIORITY":
        await _send_priority_list(sender)
    else:
        await send_text(sender, HELP)


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
    await send_voice_note(target, clip="welcome")
    await send_voice_note(target, clip="transport_reminder")
    await send_text(
        facility_number,
        f"Incoming referral #{rid}: {patient} — {danger}. Reply ARRIVED {rid} when they arrive.",
    )

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
        await send_voice_note(household.caregiver_number, clip="arrival_thanks")


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
                await send_voice_note(household.caregiver_number, clip="gentle_nudge")


def _parse_id(payload: str) -> int | None:
    try:
        return int(payload.strip().split()[0])
    except (ValueError, IndexError):
        return None
