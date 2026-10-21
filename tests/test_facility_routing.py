"""A referral goes to the facility the nurse's zone refers to. Zones with
no mapping keep the old answer, the lowest-id facility."""
import os

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_alaafei.db"

import pytest
from sqlalchemy import select

from app.db import SessionLocal, engine, ensure_raw_tables
from app.flows import referral as flow
from app.models import Base, Facility, FacilityZone, Nurse, Referral

NURSE_A = "233200000001"   # Kpalsogu CHPS
NURSE_B = "233200000004"   # Nanton CHPS
SAVELUGU = "233200000002"
NANTON = "233200000005"
CAREGIVER = "233200000003"


@pytest.fixture(autouse=True)
async def _setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        for t in ("wa_windows", "template_context", "referral_messages"):
            await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {t}")
        await conn.run_sync(Base.metadata.create_all)
    await ensure_raw_tables()
    async with SessionLocal() as s:
        s.add_all([
            Nurse(name="Abiba", wa_number=NURSE_A, chps_zone="Kpalsogu CHPS"),
            Nurse(name="Zara", wa_number=NURSE_B, chps_zone="Nanton CHPS"),
            Facility(name="Savelugu HC", wa_number=SAVELUGU, district="Savelugu"),
            Facility(name="Nanton HC", wa_number=NANTON, district="Nanton"),
        ])
        await s.commit()
    flow._PENDING.clear()
    flow._AWAITING_VOICE.clear()


@pytest.fixture
def outbox(monkeypatch):
    log: list[tuple[str, str]] = []

    async def send_text(to, body):
        log.append((to, body)); return True

    async def send_buttons(to, body, buttons):
        log.append((to, body)); return True

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(flow, "send_text", send_text)
    monkeypatch.setattr(flow, "send_buttons", send_buttons)
    monkeypatch.setattr(flow, "send_referral_template", noop)
    monkeypatch.setattr(flow, "send_facility_template", noop)
    return log


async def _facility_of(name: str) -> str:
    async with SessionLocal() as s:
        ref = (await s.execute(select(Referral).where(Referral.patient_name == name))).scalar_one()
        return (await s.get(Facility, ref.facility_id)).name


@pytest.mark.asyncio
async def test_unmapped_zone_keeps_the_lowest_id_facility(outbox):
    await flow.handle_inbound_text(NURSE_B, f"REFER Amina | fever | {CAREGIVER}")
    assert await _facility_of("Amina") == "Savelugu HC"
    assert any(to == SAVELUGU for to, _ in outbox)
    assert not any(to == NANTON for to, _ in outbox)


@pytest.mark.asyncio
async def test_mapped_zone_routes_to_its_facility(outbox):
    async with SessionLocal() as s:
        s.add(FacilityZone(chps_zone="Nanton CHPS", facility_id=2))
        await s.commit()

    await flow.handle_inbound_text(NURSE_B, f"REFER Amina | fever | {CAREGIVER}")
    await flow.handle_inbound_text(NURSE_A, f"REFER Fuseina | bleeding | {CAREGIVER}")

    assert await _facility_of("Amina") == "Nanton HC"
    assert await _facility_of("Fuseina") == "Savelugu HC"
    # The alert went to the right desk and the nurse was told which one.
    assert any(to == NANTON and "Amina" in b for to, b in outbox)
    assert any(to == NURSE_B and "-> Nanton HC" in b for to, b in outbox)
    assert not any(to == SAVELUGU and "Amina" in b for to, b in outbox)


@pytest.mark.asyncio
async def test_mapping_to_a_deleted_facility_falls_back(outbox):
    async with SessionLocal() as s:
        s.add(FacilityZone(chps_zone="Nanton CHPS", facility_id=99))
        await s.commit()
    await flow.handle_inbound_text(NURSE_B, f"REFER Amina | fever | {CAREGIVER}")
    assert await _facility_of("Amina") == "Savelugu HC"


@pytest.mark.asyncio
async def test_no_facilities_at_all_is_said_not_crashed(outbox):
    async with SessionLocal() as s:
        for f in (await s.execute(select(Facility))).scalars().all():
            await s.delete(f)
        await s.commit()
    await flow.handle_inbound_text(NURSE_A, f"REFER Amina | fever | {CAREGIVER}")
    assert any(to == NURSE_A and "No receiving facility" in b for to, b in outbox)
    async with SessionLocal() as s:
        assert (await s.execute(select(Referral))).scalars().all() == []


@pytest.mark.asyncio
async def test_map_zone_script_sets_and_shows(capsys):
    from scripts.map_zone import set_zone, show

    await set_zone("Nanton CHPS", 2)
    await set_zone("Nanton CHPS", 1)
    await show()
    out = capsys.readouterr().out
    assert "'Nanton CHPS': Savelugu HC" in out
    assert "'Kpalsogu CHPS': unmapped -> Savelugu HC" in out
