"""Voice clips live in media/ as files; WhatsApp needs them uploaded and
addressed by a media id. Meta keeps an upload for about 30 days and then
the id simply stops existing, and a send with a dead id fails with a 400.
Sixteen hard-coded ids went that way once and every voice note failed
silently for weeks.

So the id is not configuration. It is a cache: stored with its upload
date, re-uploaded before it ages out, and re-uploaded on the spot if Meta
rejects it. The file in media/ is the source of truth."""
from datetime import timedelta
from pathlib import Path

import httpx
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import MediaClip, utcnow

GRAPH = "https://graph.facebook.com/v20.0"
MEDIA_DIR = Path(__file__).resolve().parent.parent / "media"

# Meta retains uploaded media for 30 days. Refresh well before that.
MAX_AGE = timedelta(days=20)

_BASE = (
    "welcome", "transport_reminder", "danger_signs_pregnancy",
    "danger_signs_child", "arrival_thanks", "gentle_nudge",
    "feeding_6_23m", "anc_reminder",
)
CLIPS: tuple[str, ...] = _BASE + tuple(c + "_en" for c in _BASE)


def _age(ts) -> timedelta:
    now = utcnow()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=now.tzinfo)
    return now - ts


async def current_ids() -> dict[str, tuple[str, object]]:
    """clip -> (media id, uploaded_at) as stored, without uploading."""
    async with SessionLocal() as session:
        rows = (await session.execute(select(MediaClip))).scalars().all()
    return {r.clip: (r.media_id, r.uploaded_at) for r in rows}


async def upload(clip: str) -> str | None:
    """Upload media/<clip>.ogg and remember its id. None if it failed."""
    path = MEDIA_DIR / f"{clip}.ogg"
    if not path.exists() or not settings.whatsapp_access_token:
        return None
    async with httpx.AsyncClient(timeout=60) as client:
        with path.open("rb") as fh:
            r = await client.post(
                f"{GRAPH}/{settings.whatsapp_phone_number_id}/media",
                headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
                data={"messaging_product": "whatsapp", "type": "audio/ogg"},
                files={"file": (path.name, fh, "audio/ogg")},
            )
    if r.status_code != 200 or "id" not in r.json():
        print(f"[media] upload FAILED {clip}: {r.status_code} {r.text[:200]}", flush=True)
        return None
    media_id = r.json()["id"]
    await _remember(clip, media_id)
    print(f"[media] uploaded {clip} -> {media_id}", flush=True)
    return media_id


async def _remember(clip: str, media_id: str) -> None:
    async with SessionLocal() as session:
        row = await session.get(MediaClip, clip)
        if row is None:
            session.add(MediaClip(clip=clip, media_id=media_id, uploaded_at=utcnow()))
        else:
            row.media_id = media_id
            row.uploaded_at = utcnow()
        await session.commit()


async def forget(clip: str) -> None:
    """Meta said the id is not valid. Drop it so the next send re-uploads."""
    async with SessionLocal() as session:
        row = await session.get(MediaClip, clip)
        if row is not None:
            await session.delete(row)
            await session.commit()


async def media_id_for(clip: str) -> str | None:
    """A usable id for this clip: the stored one while it is young enough,
    otherwise a fresh upload."""
    if clip not in CLIPS:
        return None
    async with SessionLocal() as session:
        row = await session.get(MediaClip, clip)
    if row is not None and row.media_id and _age(row.uploaded_at) < MAX_AGE:
        return row.media_id
    return await upload(clip)


async def refresh_all(force: bool = False) -> dict[str, str | None]:
    """Upload every clip that is missing or ageing. Runs at startup and
    daily, so an id never reaches the age at which Meta drops it."""
    if not settings.whatsapp_access_token:
        return {}
    stored = await current_ids()
    out: dict[str, str | None] = {}
    for clip in CLIPS:
        have = stored.get(clip)
        if not force and have and have[0] and _age(have[1]) < MAX_AGE:
            out[clip] = have[0]
            continue
        out[clip] = await upload(clip)
    fresh = sum(1 for v in out.values() if v)
    print(f"[media] {fresh}/{len(CLIPS)} clips have a live id", flush=True)
    return out
