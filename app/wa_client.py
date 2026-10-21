"""Thin WhatsApp Cloud API client.

In dev (no access token) every send is printed instead of sent, so the whole
loop can be exercised locally and in tests without Meta credentials.
Voice notes are pre-recorded clips (a real midwife's voice) registered by
name; TTS is deliberately not used for caregiver-facing audio.
"""
import httpx

from app.config import settings
from app import media

GRAPH = "https://graph.facebook.com/v20.0"

# The clips themselves are files in media/; their WhatsApp media ids are
# uploaded, cached and refreshed by app/media.py. See CLIPS there.


async def send_text(to: str, body: str) -> bool:
    """Returns True if Meta accepted the send. False means the 24h window is
    almost certainly closed and a template is needed instead."""
    if not settings.whatsapp_access_token:
        print(f"[DEV send_text] to={to}: {body}")
        return True
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{GRAPH}/{settings.whatsapp_phone_number_id}/messages",
            headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": body},
            },
        )
    if r.status_code != 200 or "error" in r.text:
        print(f"[send_text FAILED] to={to} {r.status_code} {r.text[:200]}", flush=True)
        return False
    return True


async def send_voice_note(to: str, clip: str) -> bool:
    """Send a clip by name. The id comes from the media cache; if Meta says
    it is no longer valid, upload the file again and retry once."""
    if not settings.whatsapp_access_token:
        print(f"[DEV send_voice_note] to={to}: clip={clip}")
        return True
    media_id = await media.media_id_for(clip)
    if not media_id:
        print(f"[voice FAILED] to={to} clip={clip}: no media id", flush=True)
        return False
    r = await _post_audio(to, media_id)
    if r.status_code != 200 and "media attachment ID" in r.text:
        await media.forget(clip)
        media_id = await media.upload(clip)
        if media_id:
            r = await _post_audio(to, media_id)
    if r.status_code != 200 or "error" in r.text:
        print(f"[voice FAILED] to={to} clip={clip} {r.status_code} {r.text[:200]}", flush=True)
        return False
    return True


async def _post_audio(to: str, media_id: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=15) as client:
        return await client.post(
            f"{GRAPH}/{settings.whatsapp_phone_number_id}/messages",
            headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "audio",
                "audio": {"id": media_id},
            },
        )


async def send_voice_note_bilingual(to: str, clip: str) -> None:
    """Send the Dagbani clip, then its English twin."""
    await send_voice_note(to, clip=clip)
    await send_voice_note(to, clip=f"{clip}_en")


async def send_referral_template(to: str, caregiver_name: str, facility_name: str) -> None:
    """Cold-start a caregiver conversation. Works outside the 24h window."""
    if not settings.whatsapp_access_token:
        print(f"[DEV send_referral_template] to={to}: {caregiver_name} / {facility_name}")
        return
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{GRAPH}/{settings.whatsapp_phone_number_id}/messages",
            headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": {
                    "name": "referral_reminder",
                    "language": {"code": "en"},
                    "components": [{
                        "type": "body",
                        "parameters": [
                            {"type": "text", "parameter_name": "caregiver_name",
                             "text": caregiver_name},
                            {"type": "text", "parameter_name": "facility_name",
                             "text": facility_name},
                        ],
                    }],
                },
            },
        )
        print("[template]", r.status_code, r.text[:300])


from app.wa_window import remember_template


async def send_facility_template(
    to: str, patient_name: str, danger_sign: str, referral_id: int
) -> None:
    """Cold-start the facility desk. Their ARRIVED reply opens the window."""
    await remember_template(to, referral_id)
    if not settings.whatsapp_access_token:
        print(f"[DEV send_facility_template] to={to}: #{referral_id} {patient_name}")
        return
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{GRAPH}/{settings.whatsapp_phone_number_id}/messages",
            headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": {
                    "name": "alaafei_facility_referral",
                    "language": {"code": "en"},
                    "components": [{
                        "type": "body",
                        "parameters": [
                            {"type": "text", "parameter_name": "patient_name",
                             "text": patient_name},
                            {"type": "text", "parameter_name": "danger_sign",
                             "text": danger_sign},
                            {"type": "text", "parameter_name": "referral_id",
                             "text": str(referral_id)},
                        ],
                    }],
                },
            },
        )
        print("[facility template]", r.status_code, r.text[:250], flush=True)


async def send_nurse_template(
    to: str, patient_name: str, referral_id: int, message_summary: str
) -> None:
    """Reach a nurse whose window has shut. She is not a facility desk, so she
    gets her own wording: what was said, about whom, and nothing about
    confirming an arrival she cannot see."""
    await remember_template(to, referral_id)
    if not settings.whatsapp_access_token:
        print(f"[DEV send_nurse_template] to={to}: #{referral_id} {patient_name}")
        return
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{GRAPH}/{settings.whatsapp_phone_number_id}/messages",
            headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": {
                    "name": "alaafei_family_message",
                    "language": {"code": "en"},
                    "components": [{
                        "type": "body",
                        "parameters": [
                            {"type": "text", "parameter_name": "patient_name",
                             "text": patient_name},
                            {"type": "text", "parameter_name": "referral_id",
                             "text": str(referral_id)},
                            {"type": "text", "parameter_name": "message_summary",
                             "text": message_summary},
                        ],
                    }],
                },
            },
        )
        print("[nurse template]", r.status_code, r.text[:250], flush=True)


async def send_list(
    to: str,
    body: str,
    button_label: str,
    rows: list[tuple[str, str, str]],
    header: str | None = None,
) -> bool:
    """A single button that opens a sheet of up to 10 rows.
    rows = [(id, title, description), ...]. The tapped id comes back
    exactly as if the person typed it."""
    if not settings.whatsapp_access_token:
        print(f"[DEV send_list] to={to}: {body} {rows}")
        return True
    interactive = {
        "type": "list",
        "body": {"text": body[:1024]},
        "action": {
            "button": button_label[:20],
            "sections": [
                {
                    "rows": [
                        {
                            "id": rid[:200],
                            "title": title[:24],
                            "description": desc[:72],
                        }
                        for rid, title, desc in rows[:10]
                    ]
                }
            ],
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": header[:60]}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{GRAPH}/{settings.whatsapp_phone_number_id}/messages",
            headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "interactive",
                "interactive": interactive,
            },
        )
    if r.status_code != 200 or "error" in r.text:
        print(f"[send_list FAILED] to={to} {r.status_code} {r.text[:200]}", flush=True)
        return False
    return True


async def send_buttons(to: str, body: str, buttons: list[tuple[str, str]]) -> bool:
    """Up to 3 tappable reply buttons. buttons = [(id, label), ...].
    The tapped id comes back exactly as if the person typed it."""
    if not settings.whatsapp_access_token:
        print(f"[DEV send_buttons] to={to}: {body} {buttons}")
        return True
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{GRAPH}/{settings.whatsapp_phone_number_id}/messages",
            headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body[:1024]},
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {"id": bid, "title": label[:20]}}
                            for bid, label in buttons[:3]
                        ]
                    },
                },
            },
        )
    if r.status_code != 200 or "error" in r.text:
        print(f"[send_buttons FAILED] to={to} {r.status_code} {r.text[:200]}", flush=True)
        return False
    return True
