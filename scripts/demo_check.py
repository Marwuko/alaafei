"""Is the demo referral ready to go? One screen, no log-watching.

    python scripts/demo_check.py        # referral 20
    python scripts/demo_check.py 7      # another referral

Shows who is on each leg, whether each phone's 24h window is open (if
not, that phone must send "hi" first), whether the nurse has already
written on the thread, and the last few messages on it.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text as sql  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Facility, Household, Nurse, Referral  # noqa: E402
from app.thread import nurse_has_spoken  # noqa: E402
from app.wa_window import window_open  # noqa: E402


async def main(rid: int) -> None:
    async with SessionLocal() as s:
        r = await s.get(Referral, rid)
        if r is None:
            sys.exit(f"No referral #{rid}")
        nurse = await s.get(Nurse, r.nurse_id)
        facility = await s.get(Facility, r.facility_id)
        household = await s.get(Household, r.household_id)
        msgs = (await s.execute(sql(
            "SELECT direction, from_number, body, delivered, created_at "
            "FROM referral_messages WHERE referral_id = :r ORDER BY id DESC LIMIT 6"
        ), {"r": rid})).fetchall()

    print(f"Referral #{rid}: {r.patient_name}, {r.danger_sign}, status {r.status.value}")
    legs = [
        ("nurse", nurse.name if nurse else "?", nurse.wa_number if nurse else None),
        ("facility", facility.name if facility else "?", facility.wa_number if facility else None),
        ("household", household.caregiver_name if household else "?",
         household.caregiver_number if household else None),
    ]
    ready = True
    for leg, name, number in legs:
        if not number:
            print(f"  {leg:10s} {name}: no number")
            ready = False
            continue
        open_ = await window_open(number)
        ready &= open_
        print(f"  {leg:10s} {name} {number}: window {'OPEN' if open_ else 'SHUT - send hi'}")
    live = await nurse_has_spoken(rid)
    print(f"  thread: {'live (nurse or facility has written)' if live else 'no reply from staff yet'}")
    if msgs:
        print("  last messages:")
        for d, f, b, ok, t in reversed(msgs):
            print(f"    {str(t)[:16]} {d:11s} {f}: {b[:70]}{'' if ok else '  [held]'}")
    print("\nREADY" if ready else "\nNOT READY: every phone marked SHUT must message Alaafei first.")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 20))
