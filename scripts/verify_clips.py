"""Check that every stored media id is the clip its name says.

    python scripts/verify_clips.py            # compare Meta's copy of each id with media/<name>.ogg
    python scripts/verify_clips.py --play 2335xxxxxxx
                                              # send every clip to that number, each labelled

--check (the default) needs no phone: it downloads each stored media id
from Meta and compares it byte for byte with the file of the same name in
media/. Ids are uploaded and refreshed by the service itself (app/media.py);
a GONE here means run scripts/upload_clips.py or restart the service.
A MATCH means the id plays whatever is in that file. Whether the file
itself says what its name promises is the part only an ear can settle:
--play sends the clips one by one, each preceded by "3/16 danger_signs_child
(Dagbani), 65s", to a number that has written to Alaafei in the last 24h.
"""
import asyncio
import hashlib
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.media import CLIPS, GRAPH, current_ids  # noqa: E402

MEDIA = Path(__file__).resolve().parent.parent / "media"
VOICE_CLIPS: dict[str, str] = {}  # filled from the database in main

# What a listener should hear, so each clip can be ticked off against its
# name and not just against "it played".
SHOULD_SAY = {
    "welcome": "greets the household: a nurse has referred someone, Alaafei will keep in touch",
    "transport_reminder": "how to get to the facility, plan the fare and the vehicle, go today",
    "danger_signs_pregnancy": "danger signs in pregnancy and after birth, go at once if any",
    "danger_signs_child": "danger signs in a child, go at once if any",
    "arrival_thanks": "thank you for coming to the facility",
    "gentle_nudge": "you have not been confirmed at the facility yet, please go",
    "feeding_6_23m": "feeding a child from 6 to 23 months",
    "anc_reminder": "antenatal visit reminder",
}
HEADERS = {"Authorization": f"Bearer {settings.whatsapp_access_token}"}


def _label(name: str) -> str:
    base, lang = (name[:-3], "English") if name.endswith("_en") else (name, "Dagbani")
    return f"{base} ({lang})"


def _duration(path: Path) -> int:
    """Seconds, from the last Ogg page's granule position (Opus is 48 kHz)."""
    import struct

    data = path.read_bytes()
    last = data.rfind(b"OggS")
    return round(struct.unpack("<q", data[last + 6:last + 14])[0] / 48000)


def check() -> int:
    bad = 0
    with httpx.Client(timeout=60, headers=HEADERS) as client:
        for name, media_id in VOICE_CLIPS.items():
            local = MEDIA / f"{name}.ogg"
            if not media_id:
                print(f"NO ID     {name}")
                bad += 1
                continue
            if not local.exists():
                print(f"NO FILE   {name}  (id {media_id})")
                bad += 1
                continue
            meta = client.get(f"{GRAPH}/{media_id}")
            if meta.status_code != 200:
                print(f"GONE      {name}  id {media_id}: {meta.status_code} {meta.text[:120]}")
                bad += 1
                continue
            remote = client.get(meta.json()["url"]).content
            same = hashlib.sha256(remote).digest() == hashlib.sha256(local.read_bytes()).digest()
            if not same:
                bad += 1
            print(
                f"{'MATCH   ' if same else 'MISMATCH'}  {name:28s} id {media_id}  "
                f"local {local.stat().st_size:7d} B / {_duration(local):3d}s   "
                f"meta {len(remote):7d} B"
            )
    print(f"\n{len(VOICE_CLIPS) - bad} of {len(VOICE_CLIPS)} ids are the file their name says."
          if bad == 0 else f"\n{bad} problem(s). Run scripts/upload_clips.py, or restart the service.")
    return bad


def play(to: str) -> None:
    """Labelled playback: one text, then the clip, for every entry."""
    total = len(VOICE_CLIPS)
    with httpx.Client(timeout=30, headers=HEADERS) as client:
        url = f"{GRAPH}/{settings.whatsapp_phone_number_id}/messages"
        for i, (name, media_id) in enumerate(VOICE_CLIPS.items(), 1):
            local = MEDIA / f"{name}.ogg"
            secs = f", {_duration(local)}s" if local.exists() else ""
            base = name[:-3] if name.endswith("_en") else name
            caption = (
                f"{i}/{total}  {_label(name)}{secs}\n"
                f"Should say: {SHOULD_SAY.get(base, '?')}"
            )
            r = client.post(url, json={
                "messaging_product": "whatsapp", "to": to,
                "type": "text", "text": {"body": caption},
            })
            a = client.post(url, json={
                "messaging_product": "whatsapp", "to": to,
                "type": "audio", "audio": {"id": media_id},
            })
            ok = r.status_code == 200 and a.status_code == 200
            print(f"{'sent  ' if ok else 'FAILED'} {caption}" + ("" if ok else f"  {a.text[:120]}"))
            time.sleep(1.5)
    print(
        "\nListen on that phone and tick each one off against its label. "
        "A send that returns 200 can still be dropped if the number has not "
        "written in within 24h; watch the [status] lines in the service log."
    )


if __name__ == "__main__":
    if not settings.whatsapp_access_token:
        sys.exit("WHATSAPP_ACCESS_TOKEN is not set; run this on the box with .env")
    stored = asyncio.run(current_ids())
    VOICE_CLIPS.update({clip: stored[clip][0] if clip in stored else "" for clip in CLIPS})
    if len(sys.argv) == 3 and sys.argv[1] == "--play":
        play(sys.argv[2])
    elif len(sys.argv) == 1 or sys.argv[1] == "--check":
        sys.exit(1 if check() else 0)
    else:
        sys.exit(__doc__)
