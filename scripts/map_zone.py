"""Say which facility a CHPS zone refers to.

    python scripts/map_zone.py                       # show zones, facilities, mappings
    python scripts/map_zone.py "Kpalsogu CHPS" 1     # zone -> facility id

Referrals from nurses in an unmapped zone go to the lowest-id facility.
"""
import asyncio
import sys

from sqlalchemy import select

from app.db import SessionLocal, engine
from app.models import Base, Facility, FacilityZone, Nurse


async def show() -> None:
    async with SessionLocal() as s:
        facilities = (await s.execute(select(Facility).order_by(Facility.id))).scalars().all()
        nurses = (await s.execute(select(Nurse).order_by(Nurse.id))).scalars().all()
        mapped = {
            z.chps_zone: z.facility_id
            for z in (await s.execute(select(FacilityZone))).scalars().all()
        }
    names = {f.id: f.name for f in facilities}
    print("Facilities:")
    for f in facilities:
        print(f"  {f.id}  {f.name}  ({f.wa_number})")
    print("Zones (from registered nurses):")
    for zone in sorted({n.chps_zone for n in nurses}):
        who = ", ".join(n.name for n in nurses if n.chps_zone == zone)
        target = names.get(mapped.get(zone), "unmapped -> " + (names.get(facilities[0].id, "none") if facilities else "none"))
        print(f"  {zone!r}: {target}   [{who}]")


async def set_zone(zone: str, facility_id: int) -> None:
    async with SessionLocal() as s:
        facility = await s.get(Facility, facility_id)
        if facility is None:
            sys.exit(f"No facility with id {facility_id}")
        row = await s.get(FacilityZone, zone)
        if row is None:
            s.add(FacilityZone(chps_zone=zone, facility_id=facility_id))
        else:
            row.facility_id = facility_id
        await s.commit()
    print(f"{zone!r} -> {facility.name}")


async def main() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    if len(sys.argv) == 3:
        await set_zone(sys.argv[1], int(sys.argv[2]))
    elif len(sys.argv) == 1:
        await show()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    asyncio.run(main())
