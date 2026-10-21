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
from app import thread
from app.wa_window import last_template_referral, window_open
from app.models import (
    Facility,
    FacilityZone,
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
    send_nurse_template,
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

    # A household number is in a conversation, not at a command line. "ok",
    # "yes", "no", or a sentence that happens to open with "Transport" or
    # "Child" must reach the nurse as what was said, not trip the nurse's
    # YES-to-register or voice-guidance commands. Facilities keep ARRIVED.
    if await _is_caregiver(sender) and not await _is_facility(sender):
        await _serve_caregiver(sender, text)
        return

    if upper.startswith("REFER "):
        await _register_referral(sender, text[6:])
    elif upper.startswith("REPLY "):
        await _reply_to_family(sender, text[6:])
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
    elif upper in ("OPEN THE MESSAGE", "OPEN_THE_MESSAGE"):
        await _open_message(sender)
    elif upper in ("QUICK REPLY", "QUICK_REPLY"):
        await _quick_reply(sender)
    elif upper == "NO_ARRIVALS":
        await _no_arrivals(sender)
    elif await _looks_like_bare_reply(sender, text):
        return
    elif await _is_facility(sender):
        await _serve_facility(sender, text)
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

        facility = await _facility_for(session, nurse)
        if facility is None:
            await send_text(
                sender,
                "No receiving facility is set up yet, so this referral was not "
                "registered. Tell the district team.",
            )
            return
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
        await send_text(
            target,
            f"No number on referral #{rid}, so the two voice messages for "
            f"{patient} come to you: a welcome, then how to reach "
            f"{facility_name}. Dagbani first, then English. Play them to the "
            "household when you visit.",
        )
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


async def _facility_for(session, nurse: Nurse) -> Facility | None:
    """The facility a nurse's zone refers to. With one facility in the
    district this never mattered; with two, picking the first row sent
    every zone's patients to the same desk. Unmapped zones keep the old
    answer, the lowest-id facility, and say so in the log."""
    mapped = await session.get(FacilityZone, nurse.chps_zone)
    if mapped is not None:
        facility = await session.get(Facility, mapped.facility_id)
        if facility is not None:
            return facility
    facility = (
        await session.execute(select(Facility).order_by(Facility.id.asc()))
    ).scalars().first()
    if facility is not None:
        print(f"[route] no facility mapped for zone {nurse.chps_zone!r}, "
              f"using {facility.name}", flush=True)
    return facility


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
        # Only the facility sees the patient walk in. A nurse confirming an
        # arrival is guessing, and a wrong guess drops the referral off the
        # follow-up list entirely.
        facility = await session.get(Facility, referral.facility_id)
        if facility is None or facility.wa_number != sender:
            await send_text(
                sender,
                f"Only {facility.name if facility else 'the facility'} can "
                f"confirm that referral {rid} has arrived.",
            )
            return
        referral.status = ReferralStatus.ARRIVED
        referral.arrived_at = utcnow()
        await session.commit()
        nurse_number = (await session.get(Nurse, referral.nurse_id)).wa_number
        household = await session.get(Household, referral.household_id)
        facility_name = facility.name

    await send_text(sender, f"Referral #{rid} confirmed. Thank you.")
    await send_text(nurse_number, f"Good news: referral #{rid} ({referral.patient_name}) has arrived.")
    if household.caregiver_number:
        await _voice_with_context(
            household.caregiver_number,
            f"{referral.patient_name} has been confirmed at {facility_name} "
            f"(referral #{rid}). This message says thank you for coming. "
            "Dagbani first, then English.",
            "arrival_thanks",
        )


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
        items = await compute_priorities(session, zone=nurse.chps_zone)
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
        # One message per nurse, not one per referral. A sweep that finds six
        # overdue referrals used to send six alerts and twelve voice notes,
        # which is how a useful signal turns into something people mute.
        by_nurse: dict[str, list[str]] = {}
        first_case: dict[str, tuple[int, str]] = {}
        # caregiver number -> [(patient, facility name)] it is behind on
        nudge: dict[str, list[tuple[str, str]]] = {}
        for referral in overdue:
            referral.status = ReferralStatus.ESCALATED
            referral.escalated_at = utcnow()
            nurse = await session.get(Nurse, referral.nurse_id)
            household = await session.get(Household, referral.household_id)
            facility = await session.get(Facility, referral.facility_id)
            if nurse is None or household is None:
                continue
            first_case.setdefault(nurse.wa_number, (referral.id, referral.patient_name))
            by_nurse.setdefault(nurse.wa_number, []).append(
                f"{referral.id}. {referral.patient_name}, {referral.danger_sign} "
                f"({household.caregiver_name}, {household.community})"
            )
            if household.caregiver_number:
                nudge.setdefault(household.caregiver_number, []).append(
                    (referral.patient_name,
                     facility.name if facility else "the health facility")
                )
        await session.commit()

    hours = settings.escalation_hours
    for wa_number, lines in by_nurse.items():
        if len(lines) == 1:
            body = (
                f"Not confirmed at the facility after {hours}h:\n\n{lines[0]}\n\n"
                "Please follow up."
            )
        else:
            body = (
                f"{len(lines)} referrals not confirmed at the facility after "
                f"{hours}h:\n\n" + "\n".join(lines) + "\n\nPlease follow up."
            )
        rid, patient = first_case[wa_number]
        await _alert_nurse(
            wa_number, body, rid, patient,
            f"Not confirmed at the facility after {hours}h. Please follow up.",
        )
        print(f"[sweep] {len(lines)} overdue to {wa_number}", flush=True)

    # One nudge per number, however many referrals that number is behind on.
    for caregiver_number, cases in nudge.items():
        names = ", ".join(p for p, _ in cases[:-1])
        names = f"{names} and {cases[-1][0]}" if names else cases[-1][0]
        places = {f for _, f in cases}
        where = places.pop() if len(places) == 1 else "the health facility"
        verb = "has" if len(cases) == 1 else "have"
        await _voice_with_context(
            caregiver_number,
            f"{names} {verb} not yet been confirmed at {where}. This message "
            "asks you to go. Dagbani first, then English.",
            "gentle_nudge",
        )


# How long after the alert, then after each follow-up, the next one is due.
# Three follow-ups and then silence: a reminder that never stops is one that
# gets muted, and the referral stays on the dashboard until it is closed.
FOLLOW_UP_AFTER = [timedelta(hours=24), timedelta(hours=72), timedelta(hours=72)]


async def follow_up_escalated() -> None:
    """Runs on the same schedule as the sweep. An escalated referral is not
    done with: the nurse hears again, with different words each time, is
    asked what is stopping the person, and is shown how to close it."""
    now = utcnow()
    async with SessionLocal() as session:
        escalated = (
            await session.execute(
                select(Referral).where(
                    Referral.status == ReferralStatus.ESCALATED,
                    Referral.reminder_count < len(FOLLOW_UP_AFTER),
                )
            )
        ).scalars().all()
        # (nurse number, round) -> [(rid, patient, line)]. Grouped by round
        # too, so "last reminder" is never said over a referral on its first.
        by_nurse: dict[tuple[str, int], list[tuple[int, str, str]]] = {}
        for referral in escalated:
            since = referral.reminded_at or referral.escalated_at
            if since is None:
                continue
            if now - _as_utc(since) < FOLLOW_UP_AFTER[referral.reminder_count]:
                continue
            nurse = await session.get(Nurse, referral.nurse_id)
            household = await session.get(Household, referral.household_id)
            if nurse is None or household is None:
                continue
            referral.reminder_count += 1
            referral.reminded_at = now
            days = int((now - _as_utc(referral.escalated_at or now)).total_seconds() // 86400)
            line = (
                f"{referral.id}. {referral.patient_name}, {referral.danger_sign} "
                f"({household.caregiver_name}, {household.community}) - "
                f"{days} day{'' if days == 1 else 's'} since the alert"
            )
            by_nurse.setdefault((nurse.wa_number, referral.reminder_count), []).append(
                (referral.id, referral.patient_name, line)
            )
        await session.commit()

    for (wa_number, rounds), cases in by_nurse.items():
        rid, patient, _ = cases[0]
        listed = "\n".join(line for _, _, line in cases)
        one = len(cases) == 1
        if rounds >= len(FOLLOW_UP_AFTER):
            body = (
                f"Last reminder about {'this referral' if one else 'these referrals'}:"
                f"\n\n{listed}\n\n"
                "There will be no more messages about "
                f"{'it' if one else 'them'}, but {'it stays' if one else 'they stay'} "
                "on the dashboard until closed. Reply CLOSE and the number once "
                "the person has been seen, or no longer needs the facility."
            )
            summary = "Last reminder: still not confirmed at the facility."
        elif rounds == 2:
            body = (
                f"Still open:\n\n{listed}\n\n"
                "Is something stopping them - transport, money, someone at "
                "home? If you know, reply REPLY, the number and a message to "
                "the number on the referral, so it is on the record. Reply "
                "CLOSE and the number if it is resolved."
            )
            summary = "Still not confirmed. Is something stopping them?"
        else:
            body = (
                f"Checking in on {'this referral' if one else 'these referrals'}:"
                f"\n\n{listed}\n\n"
                "Have you been able to reach the number? Reply CLOSE and the "
                "number if the person was seen elsewhere or no longer needs "
                "the facility. Reply REPLY, the number and your message to "
                "write to them from here."
            )
            summary = "Checking in: still not confirmed at the facility."
        buttons = [(f"CLOSE {rid}", "Resolved, close it"), ("PRIORITY", "My visit list")] if one else None
        await _alert_nurse(wa_number, body, rid, patient, summary, buttons)
        print(f"[sweep] follow-up round {rounds} for {len(cases)} to {wa_number}", flush=True)


async def _alert_nurse(
    wa_number: str, body: str, rid: int, patient: str, summary: str,
    buttons: list[tuple[str, str]] | None = None,
) -> None:
    """Free text if her window is open, otherwise the nurse template. A
    doomed send returns 200, so the window is checked, never inferred."""
    if await window_open(wa_number):
        if buttons:
            await send_buttons(wa_number, body, buttons)
        else:
            await send_text(wa_number, body)
    else:
        await send_nurse_template(wa_number, patient, rid, summary)
        print(f"[sweep] nurse window shut, template sent for #{rid}", flush=True)


def _as_utc(ts):
    """SQLite hands back naive datetimes; utcnow() is aware."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=utcnow().tzinfo)
    return ts


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
        ctx = await _referral_context(sender)
        where = f"how to reach {ctx[2]}" if ctx else "how to get there"
        await send_text(
            sender,
            f"Playing again: the welcome, then {where}. Dagbani first, "
            "then English.",
        )
        await send_voice_note_bilingual(sender, clip="welcome")
        await send_voice_note_bilingual(sender, clip="transport_reminder")
        return
    if text.strip().upper() == "CARE_HELP":
        await _relay_to_facility(sender, "They tapped I need help.")
        return
    if sender in _AWAITING_VOICE:
        _AWAITING_VOICE.discard(sender)
        ctx = await _referral_context(sender)
        if ctx:
            rid, patient, facility_name = ctx
            caption = (
                f"Thank you. {patient} has been referred to {facility_name} "
                f"(referral #{rid}). Two voice messages from the nurse follow: "
                "a welcome, then how to get there. Dagbani first, then English."
            )
        else:
            caption = (
                "Thank you. Two voice messages from your nurse follow: a "
                "welcome, then how to get there. Dagbani first, then English."
            )
        await send_text(sender, caption)
        await send_voice_note_bilingual(sender, clip="welcome")
        await send_voice_note_bilingual(sender, clip="transport_reminder")
        await send_buttons(
            sender,
            "If you need anything, tap below or send a message here.",
            [("CARE_AGAIN", "Play again"),
             ("CARE_HELP", "I need help")],
        )
        # If the nurse or the desk already wrote to them, this first message
        # is their answer, not just the tap that opens the window.
        if await _thread_is_live(sender):
            await _relay_to_facility(sender, text)
        return
    plan = await understand_caregiver(text)
    if plan is None:
        # Model unavailable -- relay everything rather than drop it.
        await _relay_to_facility(sender, text)
        return
    await send_text(sender, plan["reply"])
    live = await _thread_is_live(sender)
    if plan["notify"] or live:
        note = plan["summary"] or text
        if plan["urgent"]:
            note = "URGENT: " + note
        elif not plan["notify"]:
            # An ordinary reply in a conversation the nurse started. She needs
            # her answer, so pass the family's own words straight through.
            note = text
        await _relay_to_facility(sender, note, plan.get("suggestions"))


async def _referral_context(sender: str) -> tuple[int, str, str] | None:
    """(referral id, patient, facility name) for the referral this household
    number is currently about, or None if it is on none."""
    async with SessionLocal() as session:
        row = await _latest_referral_for(session, sender)
        if row is None:
            return None
        referral = row[0]
        facility = await session.get(Facility, referral.facility_id)
        return (
            referral.id,
            referral.patient_name,
            facility.name if facility else "the health facility",
        )


async def _voice_with_context(to: str, caption: str, clip: str) -> None:
    """A bare voice note says nothing about which referral it belongs to or
    why it came, so one line of text always goes first. Both the text and
    the audio are free-form sends, which Meta drops outside the 24h window
    without saying so at send time; check first rather than fire two
    doomed messages."""
    if not await window_open(to):
        print(f"[voice] window shut for {to}, {clip} not sent", flush=True)
        return
    await send_text(to, caption)
    await send_voice_note_bilingual(to, clip=clip)


async def _relay_to_facility(
    sender: str, text: str, suggestions: list[dict] | None = None
) -> None:
    """Pass a caregiver's message to the facility and the referring nurse."""
    async with SessionLocal() as session:
        row = await _latest_referral_for(session, sender)
        referral = row[0] if row else None
        household = row[1] if row else None
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
        f"About {patient} "
        f"(referral #{rid}, {community}):\n\n"
        f'"{text}"\n\n'
        f'Type "REPLY {rid}" and your message to answer here.'
    )
    summary = " ".join(text.split())[:60]
    await thread.log(rid, "from_family", sender, text)
    # Check the window before sending. A doomed send still returns 200 and
    # the 131047 only turns up later on the status webhook, so the response
    # tells us nothing.
    if await window_open(facility_number):
        await send_text(facility_number, note)
    else:
        await send_facility_template(facility_number, patient, summary, rid)
        print(f"[relay] facility window shut, template sent for #{rid}", flush=True)
    if await window_open(nurse_number):
        # A nurse mid-shift should not have to type. Offer the drafted
        # replies as buttons, showing the exact words so she is not
        # tapping blind. The button id is the REPLY command itself.
        taps = []
        for s_ in suggestions or []:
            command = f"REPLY {rid} {s_['text']}"
            if len(command) <= 200:
                taps.append((command, s_["label"]))
        if taps:
            drafted = "\n\n".join(f'"{c.split(" ", 2)[2]}"' for c, _ in taps)
            await send_buttons(
                nurse_number,
                f"{note}\n\nTap to send one of these:\n\n{drafted}",
                taps[:3],
            )
        else:
            await send_text(nurse_number, note)
    else:
        await send_nurse_template(nurse_number, patient, rid, summary)
        print(f"[relay] nurse window shut, template sent for #{rid}", flush=True)


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
        await send_text(
            sender,
            f"Here is the {label} to play for the household. Dagbani first, "
            "then English.",
        )
        await send_voice_note_bilingual(sender, clip=clip)
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
        (
            f"{len(recent)} person was referred here and has not been confirmed. "
            if len(recent) == 1
            else f"{len(recent)} people were referred here and have not been confirmed. "
        )
        + "Open the list and pick one when they reach you.",
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
        f"Thank you. The nurses who sent these {len(pending)} people "
        "have been told nobody has reached you yet.",
    )


async def _quick_reply(sender: str) -> None:
    """The template's button sends a fixed payload, so look up what the last
    template to this number was about and treat the tap as a confirmation."""
    rid = await last_template_referral(sender)
    if rid is None:
        await send_text(
            sender,
            "Reply ARRIVED and the referral number to confirm someone has "
            "reached you.",
        )
        return
    await _confirm_arrival(sender, str(rid))



async def _reply_to_family(sender: str, payload: str) -> None:
    """REPLY 7 come to the clinic this morning -- keeps the nurse and the
    family in one thread and writes the exchange down. The receiving
    facility may answer on its own referrals as well: it is told the
    household's message and the REPLY command, so the command has to work
    for it. The nurse hears what the desk said, so her thread stays whole."""
    parts = payload.strip().split(maxsplit=1)
    rid = _parse_id(parts[0]) if parts else None
    if rid is None or len(parts) < 2:
        await send_text(sender, "Format: REPLY referral-id then your message.")
        return
    message = parts[1].strip()
    async with SessionLocal() as session:
        referral = await session.get(Referral, rid)
        if referral is None:
            await send_text(sender, f"No referral #{rid} found.")
            return
        nurse = await session.get(Nurse, referral.nurse_id)
        household = await session.get(Household, referral.household_id)
        facility = await session.get(Facility, referral.facility_id)
        copy_nurse: str | None = None
        if nurse is not None and nurse.wa_number == sender:
            signed = f"{nurse.name} (health worker)"
        elif facility is not None and facility.wa_number == sender:
            signed = facility.name
            copy_nurse = nurse.wa_number if nurse else None
        else:
            who = facility.name if facility else "the receiving facility"
            await send_text(
                sender,
                f"Only the nurse who made referral {rid} or {who} can answer on it.",
            )
            return
        caregiver_number = household.caregiver_number
        patient = referral.patient_name
    if not caregiver_number:
        await send_text(sender, f"No contact number on file for referral {rid}.")
        return
    body = f"{signed}: {message}"
    sent = await thread.deliver_or_hold(rid, caregiver_number, sender, body)
    if copy_nurse:
        await _alert_nurse(
            copy_nurse,
            f"{signed} wrote to the number on referral {rid} ({patient}):\n\n"
            f'"{message}"' + ("" if sent else "\n\n(Held until they write in.)"),
            rid, patient, f"{signed} replied: {message}"[:120],
        )
    if sent:
        await send_text(sender, f"Sent on referral {rid} for {patient}.")
    else:
        await send_text(
            sender,
            f"The number on referral {rid} has not written in for over a "
            "day, so WhatsApp will not let us message it right now. Your "
            "message is saved and goes out the moment they write back.",
        )


async def _thread_is_live(sender: str) -> bool:
    """True when a nurse has already written to this family about their
    referral. Silence after she asks a question is the worst outcome."""
    async with SessionLocal() as session:
        row = await _latest_referral_for(session, sender)
        if row is None:
            return False
        rid = row[0].id
    return await thread.nurse_has_spoken(rid)


async def _looks_like_bare_reply(sender: str, text: str) -> bool:
    """A nurse typing "7 bring her ANC card" meant REPLY 7. Losing her message
    to the generic menu is the worst answer, so offer the corrected command as
    a button. Tapping it sends the exact text she already wrote."""
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[0].isdigit():
        return False
    rid = int(parts[0])
    message = parts[1].strip()
    async with SessionLocal() as session:
        referral = await session.get(Referral, rid)
        if referral is None:
            return False
        nurse = await session.get(Nurse, referral.nurse_id)
        facility = await session.get(Facility, referral.facility_id)
        mine = (nurse is not None and nurse.wa_number == sender) or (
            facility is not None and facility.wa_number == sender
        )
        if not mine:
            return False
        patient = referral.patient_name
    # Only offer this where a conversation already exists. Otherwise any
    # sentence that opens with a number gets read as a message to a family.
    if not await thread.nurse_has_spoken(rid):
        return False
    command = f"REPLY {rid} {message}"
    if len(command) > 200:
        await send_text(
            sender,
            f"To answer on referral {rid} for {patient}, start your "
            f"message with REPLY {rid} and send it again.",
        )
        return True
    await send_buttons(
        sender,
        f"Send this on referral {rid} for {patient}?\n\n{message}",
        [(command, "Send to family"), ("CANCEL", "No")],
    )
    return True


async def _open_message(sender: str) -> None:
    """The nurse template button. Tapping it reopens her window, so all that
    is left is to tell her how to answer."""
    rid = await last_template_referral(sender)
    if rid is None:
        await send_text(sender, "Type REPLY and the referral number to answer.")
        return
    async with SessionLocal() as session:
        referral = await session.get(Referral, rid)
        patient = referral.patient_name if referral else None
    if patient is None:
        await send_text(sender, f"Type REPLY {rid} and your message to answer.")
        return
    await send_text(
        sender,
        f"Referral {rid} for {patient}. Type REPLY {rid} and your message to "
        "answer, and it will reach them from this number.",
    )


async def _latest_referral_for(session, sender: str):
    """Which referral is this number talking about?

    One number can sit on several households and several referrals over time.
    Answering with the oldest one means a message about today gets filed
    against something from last week. Prefer the newest referral that is still
    open; fall back to the newest of any kind so a reply after discharge still
    lands somewhere sensible.
    """
    base = (
        select(Referral, Household)
        .join(Household, Household.id == Referral.household_id)
        .where(Household.caregiver_number == sender)
    )
    still_open = (
        await session.execute(
            base.where(
                Referral.status.notin_(
                    [ReferralStatus.ARRIVED, ReferralStatus.CLOSED]
                )
            ).order_by(Referral.id.desc())
        )
    ).first()
    if still_open is not None:
        return still_open
    return (await session.execute(base.order_by(Referral.id.desc()))).first()
