"""Understand a nurse's plain-language referral.

Nurses in the field should not have to remember pipe syntax. This turns
free text — in English, Dagbani-inflected English, or a mix — into the
three fields a referral needs. Clinical prioritisation stays in
rules.py, where a midwife can audit it; the model only reads intent.
"""
import json

from anthropic import AsyncAnthropic

from app.config import settings

SYSTEM = """You extract referral details from a community health nurse's message.

Return ONLY a JSON object, no prose, no markdown fences:
{"patient": str, "danger_sign": str, "caregiver_number": str or null}

Rules:
- patient: the person being referred. If no name is given, use null.
- danger_sign: the clinical concern, in a few words, as the nurse described it.
- caregiver_number: any phone number in the message, digits only. Convert a
  leading 0 to Ghana country code 233 (0242675709 -> 233242675709). If no
  number appears, use null.
- If the message is not a referral at all, return {"patient": null}."""


async def parse_referral(text: str) -> dict | None:
    """Return parsed fields, or None if unavailable or not a referral."""
    if not settings.anthropic_api_key:
        return None
    try:
        client = AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=8.0)
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
        raw = "".join(b.text for b in msg.content if b.type == "text").strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(raw)
    except Exception as exc:  # network, timeout, bad JSON — fall back to HELP
        print(f"[parse_referral] failed: {exc}")
        return None
    if not isinstance(data, dict) or not data.get("patient"):
        return None
    return {
        "patient": str(data["patient"]).strip(),
        "danger_sign": str(data.get("danger_sign") or "unspecified").strip(),
        "caregiver_number": (
            str(data["caregiver_number"]).strip() if data.get("caregiver_number") else None
        ),
    }
