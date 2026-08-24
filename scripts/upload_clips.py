"""Upload Naomi's voice notes to WhatsApp media and write IDs into wa_client.py."""
import os
import re

import httpx

TOKEN = ""
PHONE_ID = ""
with open(".env") as f:
    for line in f:
        if line.startswith("WHATSAPP_ACCESS_TOKEN="):
            TOKEN = line.split("=", 1)[1].strip()
        if line.startswith("WHATSAPP_PHONE_NUMBER_ID="):
            PHONE_ID = line.split("=", 1)[1].strip()

_BASE = [
    "welcome", "transport_reminder", "danger_signs_pregnancy",
    "danger_signs_child", "arrival_thanks", "gentle_nudge",
    "feeding_6_23m", "anc_reminder",
]
CLIPS = _BASE + [c + "_en" for c in _BASE]

ids = {}
for clip in CLIPS:
    path = f"media/{clip}.ogg"
    if not os.path.exists(path):
        print(f"{clip}: file not found, skipped")
        ids[clip] = ""
        continue
    with open(path, "rb") as fh:
        r = httpx.post(
            f"https://graph.facebook.com/v20.0/{PHONE_ID}/media",
            headers={"Authorization": f"Bearer {TOKEN}"},
            data={"messaging_product": "whatsapp", "type": "audio/ogg"},
            files={"file": (f"{clip}.ogg", fh, "audio/ogg")},
            timeout=60,
        )
    if r.status_code == 200:
        ids[clip] = r.json()["id"]
        print(f"{clip}: uploaded, id {ids[clip]}")
    else:
        ids[clip] = ""
        print(f"{clip}: FAILED {r.status_code} {r.text[:100]}")

block = "VOICE_CLIPS: dict[str, str] = {\n"
for clip in CLIPS:
    block += f'    "{clip}": "{ids[clip]}",\n'
block += "}"

p = "app/wa_client.py"
src = open(p).read()
src = re.sub(r"VOICE_CLIPS: dict\[str, str\] = \{.*?\}", block, src, flags=re.S)
open(p, "w").write(src)
print("\nwa_client.py updated with media IDs")
