"""Upload every clip in media/ to WhatsApp and store the ids.

    python scripts/upload_clips.py          # upload what is missing or ageing
    python scripts/upload_clips.py --force  # upload all sixteen again

The running service does this itself at startup and once a day; this is
for doing it by hand, for example right after replacing a recording.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import engine, ensure_raw_tables  # noqa: E402
from app.media import CLIPS, refresh_all  # noqa: E402
from app.models import Base  # noqa: E402


async def main() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_raw_tables()
    ids = await refresh_all(force="--force" in sys.argv)
    if not ids:
        sys.exit("WHATSAPP_ACCESS_TOKEN is not set; run this on the box with .env")
    for clip in CLIPS:
        print(f"{clip:28s} {ids.get(clip) or 'FAILED'}")


if __name__ == "__main__":
    asyncio.run(main())
