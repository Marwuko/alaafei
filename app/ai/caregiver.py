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
{"reply": str, "notify": bool, "summary": str, "urgent": bool,
 "suggestions": [{"label": str, "text": str}]}

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

- suggestions: up to 2 replies the NURSE could send back to this person,
  written in her voice, for her to tap instead of typing. Only when
  notify is true; otherwise an empty list.
  label: 16 characters MAXIMUM, hard limit, plain, what tapping it does
  ("Come in today", "Ask what they can pay").
  text: the full sentence the person receives, under 25 words, warm and
  clear.
  ONLY logistics, reassurance, or a question: when to come, how to get
  there, what to bring, asking what is stopping them.
  NEVER clinical. No symptom assessment, no advice on what to do for a
  symptom, no drugs, no doses, no "that is normal", no "do not worry
  about it". If the only useful reply would be clinical, return an empty
  list and let the nurse write it herself.
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
        "suggestions": _clean_suggestions(data.get("suggestions")),
    }


def _clean_suggestions(raw) -> list[dict]:
    """Trust nothing from the model: shape, length, and count all enforced."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:2]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        text = str(item.get("text") or "").strip()
        if not label or not text:
            continue
        if len(label) > 20:
            # Truncating mid-word gives a nurse "Offer to arrange tra"
            label = label[:20].rsplit(" ", 1)[0]
        out.append({"label": label, "text": " ".join(text.split())[:160]})
    return out
