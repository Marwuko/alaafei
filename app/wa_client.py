"""Thin WhatsApp Cloud API client.

In dev (no access token) every send is printed instead of sent, so the whole
loop can be exercised locally and in tests without Meta credentials.
Voice notes are pre-recorded clips (a real midwife's voice) registered by
name; TTS is deliberately not used for caregiver-facing audio.
"""
import httpx

from app.config import settings

GRAPH = "https://graph.facebook.com/v20.0"

# clip name -> uploaded media id (filled in after uploading Naomi's recordings)
VOICE_CLIPS: dict[str, str] = {
    "welcome": "",
    "transport_reminder": "",
    "danger_signs_pregnancy": "",
    "danger_signs_child": "",
    "arrival_thanks": "",
    "gentle_nudge": "",
    "feeding_6_23m": "",
    "anc_reminder": "",
}


async def send_text(to: str, body: str) -> None:
    if not settings.whatsapp_access_token:
        print(f"[DEV send_text] to={to}: {body}")
        return
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"{GRAPH}/{settings.whatsapp_phone_number_id}/messages",
            headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": body},
            },
        )


async def send_voice_note(to: str, clip: str) -> None:
    media_id = VOICE_CLIPS.get(clip, "")
    if not settings.whatsapp_access_token or not media_id:
        print(f"[DEV send_voice_note] to={to}: clip={clip}")
        return
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"{GRAPH}/{settings.whatsapp_phone_number_id}/messages",
            headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "audio",
                "audio": {"id": media_id},
            },
        )
