"""Media ids are a cache with an expiry, not configuration. A missing or
ageing id is re-uploaded before use; an id Meta rejects is dropped,
re-uploaded and the send retried once; a failed send is logged, not
swallowed."""
import os

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_alaafei.db"

from datetime import timedelta

import pytest

from app import media, wa_client
from app.config import settings
from app.db import SessionLocal, engine, ensure_raw_tables
from app.models import Base, MediaClip, utcnow


class FakeResponse:
    def __init__(self, status, text):
        self.status_code, self.text = status, text


@pytest.fixture(autouse=True)
async def _setup(monkeypatch):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await ensure_raw_tables()
    monkeypatch.setattr(settings, "whatsapp_access_token", "test-token")
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "1")


@pytest.fixture
def uploads(monkeypatch):
    """Stand in for the Meta upload: records calls, hands out fresh ids."""
    calls: list[str] = []

    async def upload(clip):
        calls.append(clip)
        mid = f"new-{clip}-{len(calls)}"
        await media._remember(clip, mid)
        return mid

    monkeypatch.setattr(media, "upload", upload)
    return calls


async def _store(clip, mid, age_days=0):
    async with SessionLocal() as s:
        s.add(MediaClip(clip=clip, media_id=mid,
                        uploaded_at=utcnow() - timedelta(days=age_days)))
        await s.commit()


@pytest.mark.asyncio
async def test_missing_id_is_uploaded_once_then_reused(uploads):
    assert await media.media_id_for("welcome") == "new-welcome-1"
    assert await media.media_id_for("welcome") == "new-welcome-1"
    assert uploads == ["welcome"]


@pytest.mark.asyncio
async def test_ageing_id_is_replaced_before_meta_drops_it(uploads):
    await _store("welcome", "old", age_days=21)
    await _store("gentle_nudge", "fine", age_days=5)
    assert await media.media_id_for("welcome") == "new-welcome-1"
    assert await media.media_id_for("gentle_nudge") == "fine"
    assert uploads == ["welcome"]


@pytest.mark.asyncio
async def test_unknown_clip_name_is_refused(uploads):
    assert await media.media_id_for("not_a_clip") is None
    assert uploads == []


@pytest.mark.asyncio
async def test_refresh_all_touches_only_what_needs_it(uploads):
    await _store("welcome", "old", age_days=25)
    await _store("welcome_en", "fine", age_days=1)
    ids = await media.refresh_all()
    assert ids["welcome"] == "new-welcome-1"
    assert ids["welcome_en"] == "fine"
    assert len(ids) == len(media.CLIPS)
    assert "welcome_en" not in uploads and "welcome" in uploads
    assert len(uploads) == len(media.CLIPS) - 1


@pytest.mark.asyncio
async def test_rejected_id_is_reuploaded_and_the_send_retried(uploads, monkeypatch, capsys):
    await _store("welcome", "dead", age_days=3)
    posted: list[str] = []

    async def post_audio(to, media_id):
        posted.append(media_id)
        if media_id == "dead":
            return FakeResponse(400, '{"error":{"message":"Param audio.id is not a '
                                     'valid whatsapp business account media attachment ID"}}')
        return FakeResponse(200, '{"messages":[{"id":"wamid.x"}]}')

    monkeypatch.setattr(wa_client, "_post_audio", post_audio)

    assert await wa_client.send_voice_note("2330", "welcome") is True
    assert posted == ["dead", "new-welcome-1"]
    assert uploads == ["welcome"]
    assert await media.media_id_for("welcome") == "new-welcome-1"


@pytest.mark.asyncio
async def test_other_failures_are_logged_not_swallowed(uploads, monkeypatch, capsys):
    await _store("welcome", "ok", age_days=1)

    async def post_audio(to, media_id):
        return FakeResponse(400, '{"error":{"message":"(#131047) Re-engagement message"}}')

    monkeypatch.setattr(wa_client, "_post_audio", post_audio)

    assert await wa_client.send_voice_note("2330", "welcome") is False
    assert uploads == []
    assert "[voice FAILED]" in capsys.readouterr().out
