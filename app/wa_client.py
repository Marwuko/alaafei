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
    "welcome": "2835447660157535",
    "transport_reminder": "2300158304082115",
    "danger_signs_pregnancy": "1583415133238627",
    "danger_signs_child": "1388906453345789",
    "arrival_thanks": "2522061161625087",
    "gentle_nudge": "1543193877065027",
    "feeding_6_23m": "1083919747695152",
    "anc_reminder": "2243884623072950",
    "welcome_en": "1623342495798409",
    "transport_reminder_en": "1586146593177142",
    "danger_signs_pregnancy_en": "27969055916108626",
    "danger_signs_child_en": "1559679862861992",
    "arrival_thanks_en": "1720349382544596",
    "gentle_nudge_en": "28024960783824492",
    "feeding_6_23m_en": "3070504789809648",
    "anc_reminder_en": "25889003290796652",
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


async def send_voice_note_bilingual(to: str, clip: str) -> None:
    """Send the Dagbani clip, then its English twin."""
    await send_voice_note(to, clip=clip)
    await send_voice_note(to, clip=f"{clip}_en")
