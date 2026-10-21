"""Seed clearly-synthetic demo data for the bootcamp demo."""
import asyncio

from app.db import SessionLocal, engine
from app.models import Base, Facility, FacilityZone, Nurse


async def main() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        session.add_all(
            [
                Nurse(name="Demo Nurse Abiba", wa_number="DEMO_NURSE_NUMBER", chps_zone="Kpalsogu CHPS"),
                Facility(name="Savelugu Health Centre", wa_number="DEMO_FACILITY_NUMBER", district="Savelugu"),
            ]
        )
        await session.flush()
        session.add(FacilityZone(chps_zone="Kpalsogu CHPS", facility_id=1))
        await session.commit()
    print("Seeded: 1 nurse, 1 facility, Kpalsogu CHPS -> facility 1. Replace DEMO_* numbers with the real demo phones.")


if __name__ == "__main__":
    asyncio.run(main())
