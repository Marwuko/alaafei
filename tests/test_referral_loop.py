"""End-to-end: register -> notify -> confirm; and escalation of overdue."""
import os

# Tests get their own throwaway database — never the dev/live one.
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_alaafei.db"


from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db import SessionLocal, engine
from app.flows import referral as flow
from app.models import Base, Facility, Nurse, Referral, ReferralStatus, utcnow

NURSE = "233200000001"
FACILITY = "233200000002"
CAREGIVER = "233200000003"


@pytest.fixture(autouse=True)
async def _setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as s:
        s.add_all(
            [
                Nurse(name="Abiba", wa_number=NURSE, chps_zone="Kpalsogu CHPS"),
                Facility(name="Savelugu HC", wa_number=FACILITY, district="Savelugu"),
            ]
        )
        await s.commit()


@pytest.mark.asyncio
async def test_full_loop():
    await flow.handle_inbound_text(NURSE, f"REFER Amina | heavy bleeding | {CAREGIVER}")
    async with SessionLocal() as s:
        ref = (await s.execute(select(Referral))).scalar_one()
        assert ref.status == ReferralStatus.CAREGIVER_NOTIFIED

    await flow.handle_inbound_text(FACILITY, f"ARRIVED {ref.id}")
    async with SessionLocal() as s:
        ref = await s.get(Referral, ref.id)
        assert ref.status == ReferralStatus.ARRIVED
        assert ref.arrived_at is not None


@pytest.mark.asyncio
async def test_escalation():
    await flow.handle_inbound_text(NURSE, f"REFER Fusheini | fast breathing | {CAREGIVER}")
    async with SessionLocal() as s:
        ref = (await s.execute(select(Referral))).scalar_one()
        ref.notified_at = utcnow() - timedelta(hours=72)
        await s.commit()

    await flow.escalate_overdue()
    async with SessionLocal() as s:
        ref = await s.get(Referral, ref.id)
        assert ref.status == ReferralStatus.ESCALATED


@pytest.mark.asyncio
async def test_priority_ranking():
    from app.prioritization.rules import compute_priorities

    await flow.handle_inbound_text(NURSE, f"REFER Amina | heavy bleeding | {CAREGIVER}")
    await flow.handle_inbound_text(NURSE, "REFER Fusheini | mild cough | ")
    await flow.handle_inbound_text(NURSE, f"REFER Zeinab | fever | {CAREGIVER}")

    async with SessionLocal() as s:
        refs = (await s.execute(select(Referral))).scalars().all()
        by_name = {r.patient_name: r for r in refs}
        by_name["Amina"].notified_at = utcnow() - timedelta(hours=72)
        await s.commit()

    await flow.escalate_overdue()

    async with SessionLocal() as s:
        items = await compute_priorities(s)

    assert items[0].patient_name == "Amina"
    assert any("never confirmed" in r for r in items[0].reasons)
    fusheini = next(i for i in items if i.patient_name == "Fusheini")
    assert any("no phone" in r for r in fusheini.reasons)


@pytest.mark.asyncio
async def test_priority_command_for_unknown_number():
    await flow.handle_inbound_text("233000000000", "PRIORITY")


@pytest.mark.asyncio
async def test_dashboard_stats():
    from app.dashboard import compute_stats

    await flow.handle_inbound_text(NURSE, f"REFER Amina | heavy bleeding | {CAREGIVER}")
    await flow.handle_inbound_text(NURSE, f"REFER Zeinab | fever | {CAREGIVER}")
    async with SessionLocal() as s:
        ref = (await s.execute(select(Referral))).scalars().first()
    await flow.handle_inbound_text(FACILITY, f"ARRIVED {ref.id}")

    async with SessionLocal() as s:
        stats, rows = await compute_stats(s)
    assert stats.total == 2
    assert stats.arrived == 1
    assert stats.completion_pct == 50
    assert len(rows) == 2
