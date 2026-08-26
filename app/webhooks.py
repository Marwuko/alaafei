"""WhatsApp Cloud API webhook endpoints.

Three guarantees, in order of importance:
1. GET verification so Meta accepts the callback URL.
2. HMAC-SHA256 signature check so only Meta can talk to us.
3. Idempotency by wa_message_id so WhatsApp's retry storms never
   double-register a referral.
"""
import hashlib
import hmac

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import SessionLocal
from app.flows.referral import handle_inbound_text
from app.models import MessageLog
from app.thread import flush as thread_flush
from app.wa_window import mark_inbound

router = APIRouter()


@router.get("/webhook")
async def verify(
    mode: str = Query(alias="hub.mode", default=""),
    token: str = Query(alias="hub.verify_token", default=""),
    challenge: str = Query(alias="hub.challenge", default=""),
):
    if mode == "subscribe" and token == settings.whatsapp_verify_token:
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="verification failed")


def _valid_signature(payload: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(
        settings.whatsapp_app_secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


@router.post("/webhook")
async def inbound(request: Request):
    raw = await request.body()
    if not _valid_signature(raw, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=403, detail="bad signature")

    data = await request.json()
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                await _process_once(msg)
            for st in value.get("statuses", []):
                errs = st.get("errors") or []
                print(f"[status] {st.get('status')} to={st.get('recipient_id')} "
                      f"id={st.get('id','')[-12:]} errors={errs}", flush=True)
    # Always 200 fast: slow responses trigger Meta retries.
    return {"status": "ok"}


async def _process_once(msg: dict) -> None:
    wa_id = msg.get("id", "")
    sender = msg.get("from", "")
    body = (msg.get("text") or {}).get("body", "")
    if not body:
        # Button taps arrive as interactive replies, not text.
        inter = msg.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        body = reply.get("id", "")
    if not body and msg.get("button"):
        # Template quick-reply buttons come through differently again.
        body = msg["button"].get("payload") or msg["button"].get("text", "")
    if not wa_id or not sender:
        return

    await mark_inbound(sender)
    await thread_flush(sender)

    async with SessionLocal() as session:
        session.add(MessageLog(wa_message_id=wa_id, from_number=sender, body=body))
        try:
            await session.commit()
        except IntegrityError:
            # Seen this message before — WhatsApp retry. Skip silently.
            await session.rollback()
            return

    if body:
        await handle_inbound_text(sender=sender, body=body)
