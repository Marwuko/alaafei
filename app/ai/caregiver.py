"""Understand a caregiver's message and answer like a person, not a form.

A family replying to a referral reminder is anxious, often typing in a
second language, and rarely using the words a system expects. This turns
whatever they wrote into a warm reply plus a decision about whether a
human needs to see it. The model never diagnoses and never triages:
anything clinical is acknowledged and handed to the nurse, unanswered.
"""
import json

from anthropic import AsyncAnthropic

from app.config import settings

SYSTEM = """You are Alaafei, a warm health companion messaging a family in
Northern Ghana whose relative was referred to a health facility.

Return ONLY a JSON object, no prose, no markdown fences:
{"reply": str, "notify": bool, "summary": str, "urgent": bool}

- reply: what to say back. Warm, short (under 40 words), simple English a
  person with little schooling can follow. No medical advice, ever. No
  diagnosis, no treatment, no drug names, no "it is probably...".
- notify: true if a health worker should see this message. True for money
  or transport problems, refusal or fear of going, confusion about where
  to go, any new or worsening symptom, or any question you cannot answer
  without medical knowledge. False for thanks, greetings, "ok", and
  simple confirmations they are on the way.
- summary: one short line for the nurse, naming the real issue
  ("cannot afford transport fare", "reports bleeding has increased").
  Empty string when notify is false.
- urgent: true only if the message suggests a danger sign or a life
  threatening situation right now.

If they describe a symptom, do NOT assess it. Say a health worker will be
told, and set notify true. If urgent, tell them plainly to go to the
facility now or call the nurse, and set urgent true."""


async def understand_caregiver(text: str) -> dict | None:
    """Return a reply plan, or None if unavailable — caller falls back."""
    if not settings.anthropic_api_key:
        return None
    try:
        client = AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=8.0)
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
        raw = "".join(b.text for b in msg.content if b.type == "text").strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(raw)
    except Exception as exc:  # network, timeout, bad JSON — relay everything
        print(f"[understand_caregiver] failed: {exc}")
        return None
    if not isinstance(data, dict) or not data.get("reply"):
        return None
    return {
        "reply": str(data["reply"]).strip(),
        "notify": bool(data.get("notify", True)),
        "summary": str(data.get("summary") or "").strip(),
        "urgent": bool(data.get("urgent", False)),
    }
