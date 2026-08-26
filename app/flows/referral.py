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
from app.ai.caregiver import understand_caregiver
from app.ai.parse import parse_referral
from app.wa_client import (
    send_buttons,
    send_list,
    send_facility_template,
    send_referral_template,
    send_text,
    send_voice_note,
    send_voice_note_bilingual,
)

# sender -> parsed referral awaiting YES confirmation
_PENDING: dict[str, dict] = {}
# caregiver numbers that were sent a template and owe us a first reply
_AWAITING_VOICE: set[str] = set()

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
    elif upper == "MENU_REFER":
        await send_text(
            sender,
            "Type the referral in your own words, for example:\n\n"
            "Fuseina at Kpalsogu is bleeding heavily, "
            "call her husband on 0242675709\n\n"
            "Or use: REFER name | danger sign | number",
        )
    elif upper == "MENU_VOICE":
        await send_buttons(
            sender,
            "Which voice guidance? It will come to you in Dagbani and English, "
            "or add a number to send it to a family.",
            [("FEEDING", "Feeding 6-23m"),
             ("DANGER", "Danger signs"),
             ("TRANSPORT", "Getting there")],
        )
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
    elif upper == "NO_ARRIVALS":
        await _no_arrivals(sender)
    elif await _is_facility(sender):
        await _serve_facility(sender, text)
    elif await _is_caregiver(sender):
        await _serve_caregiver(sender, text)
    else:
        parsed = await parse_referral(text)
        if parsed is None:
            await send_buttons(
                sender,
                "I did not understand that. What would you like to do?",
                [("PRIORITY", "My visit list"),
                 ("MENU_REFER", "Make a referral"),
                 ("MENU_VOICE", "Send guidance")],
            )
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
        nurse_name = nurse.name
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
        _AWAITING_VOICE.add(caregiver_number)
    else:
        await send_voice_note_bilingual(target, clip="welcome")
        await send_voice_note_bilingual(target, clip="transport_reminder")
    ok = await send_buttons(
        facility_number,
        f"Incoming referral #{rid}: {patient} — {danger}.\n"
        f"From {nurse_name}. Tap below when she reaches you.",
        [(f"ARRIVED {rid}", "She has arrived")],
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
    if text.strip().upper() == "CARE_AGAIN":
        await send_voice_note_bilingual(sender, clip="welcome")
        await send_voice_note_bilingual(sender, clip="transport_reminder")
        return
    if text.strip().upper() == "CARE_HELP":
        await _relay_to_facility(sender, "The family says they need help.")
        return
    if sender in _AWAITING_VOICE:
        _AWAITING_VOICE.discard(sender)
        await send_text(
            sender,
            "Thank you. Here is a message from your nurse - it plays in "
            "Dagbani, then English.",
        )
        await send_voice_note_bilingual(sender, clip="welcome")
        await send_voice_note_bilingual(sender, clip="transport_reminder")
        await send_buttons(
            sender,
            "If you need anything, tap below or send a message here.",
            [("CARE_AGAIN", "Play again"),
             ("CARE_HELP", "I need help")],
        )
        return
    plan = await understand_caregiver(text)
    if plan is None:
        # Model unavailable -- relay everything rather than drop it.
        await _relay_to_facility(sender, text)
        return
    await send_text(sender, plan["reply"])
    if plan["notify"]:
        note = plan["summary"] or text
        if plan["urgent"]:
            note = "URGENT: " + note
        await _relay_to_facility(sender, note)


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
        rid = referral.id
        patient = referral.patient_name
        community = household.community
        facility_number = facility.wa_number
        nurse_number = nurse.wa_number

    note = (
        f"Message from the family of {patient} "
        f"(referral #{rid}, {community}):\n\n"
        f'"{text}"\n\n'
        f"Reply to them on {sender}."
    )
    reached = await send_text(facility_number, note)
    if not reached:
        # 24h window shut. The template is the only way through.
        await send_facility_template(
            facility_number, patient, " ".join(text.split())[:60], rid
        )
        print(f"[relay] window closed, template sent for #{rid}", flush=True)
    await send_text(nurse_number, note)


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


async def _is_facility(sender: str) -> bool:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(Facility).where(Facility.wa_number == sender)
            )
        ).scalars().first()
    return row is not None


async def _serve_facility(sender: str, text: str) -> None:
    """A facility desk is not a nurse. It confirms arrivals, nothing else."""
    async with SessionLocal() as session:
        facility = (
            await session.execute(
                select(Facility).where(Facility.wa_number == sender)
            )
        ).scalars().first()
        fname = facility.name
        rows = (
            await session.execute(
                select(Referral)
                .where(Referral.facility_id == facility.id)
                .where(
                    Referral.status.notin_(
                        [ReferralStatus.ARRIVED, ReferralStatus.CLOSED]
                    )
                )
                .order_by(Referral.id.asc())
                .limit(9)
            )
        ).scalars().all()
        recent = [
            (
                r.id,
                r.patient_name,
                r.danger_sign,
                getattr(r, "referred_at", None) or getattr(r, "created_at", None),
            )
            for r in rows
        ]
    if not recent:
        await send_text(
            sender,
            f"{fname}: everyone referred has been confirmed. "
            "You will get a message here when a nurse sends someone.",
        )
        return
    now = utcnow()
    listrows = []
    for rid, name, sign, ts in recent:
        listrows.append((f"ARRIVED {rid}", f"{name} #{rid}", f"{sign}{_waited(ts, now)}"))
    listrows.append(
        ("NO_ARRIVALS", "None have come yet", "Tell the nurses nobody has arrived")
    )
    await send_list(
        sender,
        f"{len(recent)} people were referred here and have not been "
        "confirmed. Open the list and pick one when they reach you.",
        "Confirm arrival",
        listrows,
        header=fname,
    )


def _waited(ts, now) -> str:
    if ts is None:
        return ""
    # SQLite hands back naive datetimes; utcnow() is aware.
    if ts.tzinfo is None and now.tzinfo is not None:
        ts = ts.replace(tzinfo=now.tzinfo)
    elif ts.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=ts.tzinfo)
    hours = (now - ts).total_seconds() / 3600
    if hours < 1:
        return " · just now"
    if hours < 24:
        return f" · waiting {int(hours)}h"
    return f" · waiting {int(hours // 24)}d"


async def _no_arrivals(sender: str) -> None:
    """The desk says nobody has turned up. Tell the referring nurses now
    instead of waiting for the escalation sweep."""
    async with SessionLocal() as session:
        facility = (
            await session.execute(
                select(Facility).where(Facility.wa_number == sender)
            )
        ).scalars().first()
        rows = (
            await session.execute(
                select(Referral)
                .where(Referral.facility_id == facility.id)
                .where(
                    Referral.status.notin_(
                        [ReferralStatus.ARRIVED, ReferralStatus.CLOSED]
                    )
                )
            )
        ).scalars().all()
        pending = []
        for r in rows:
            r.status = ReferralStatus.ESCALATED
            r.escalated_at = utcnow()
            nurse = await session.get(Nurse, r.nurse_id)
            household = await session.get(Household, r.household_id)
            pending.append(
                (nurse.wa_number, r.id, r.patient_name, household.caregiver_name,
                 household.community)
            )
        await session.commit()
        fname = facility.name
    for wa_number, rid, patient, caregiver, community in pending:
        await send_text(
            wa_number,
            f"{fname} says referral {rid} for {patient} has still not "
            f"arrived. Please follow up with {caregiver} in {community}.",
        )
    await send_text(
        sender,
        f"Thank you. {len(pending)} nurses have been told nobody has "
        "reached you yet.",
    )
