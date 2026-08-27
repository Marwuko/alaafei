"""Morning priority list — transparent rules, no black box.

Every score is a sum of named signals, and every signal carries a
plain-language reason the nurse can read. This is deliberate: the nurse
must always be able to answer "why is this household first?" — to
herself, to the family, and to her supervisor. ML can come later,
trained on the data this system generates; day one runs on rules a
midwife can audit.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import Household, Referral, ReferralStatus, utcnow, Nurse

HIGH_RISK_SIGNS = (
    "bleed",
    "convuls",
    "fit",
    "breath",
    "asphyxia",
    "unconscious",
    "fever",
)


def _as_utc(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes; treat them as UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


@dataclass
class PriorityItem:
    referral_id: int
    patient_name: str
    community: str
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    def add(self, points: int, reason: str) -> None:
        self.score += points
        self.reasons.append(reason)


async def compute_priorities(
    session, limit: int = 5, zone: str | None = None
) -> list[PriorityItem]:
    """Rank open referrals for a nurse's morning visit list."""
    now = utcnow()
    open_referrals = (
        (
            await session.execute(
                (
                    select(Referral)
                    .join(Nurse, Nurse.id == Referral.nurse_id)
                    .where(Nurse.chps_zone == zone)
                    if zone
                    else select(Referral)
                ).where(
                    Referral.status.in_(
                        [
                            ReferralStatus.REGISTERED,
                            ReferralStatus.CAREGIVER_NOTIFIED,
                            ReferralStatus.ESCALATED,
                        ]
                    )
                )
            )
        )
        .scalars()
        .all()
    )

    items: list[PriorityItem] = []
    for ref in open_referrals:
        household = await session.get(Household, ref.household_id)
        item = PriorityItem(
            referral_id=ref.id,
            patient_name=ref.patient_name,
            community=household.community if household else "unknown",
        )

        if ref.status == ReferralStatus.ESCALATED:
            item.add(100, "this referral was never confirmed at the facility. Please follow up first")
        elif ref.status == ReferralStatus.CAREGIVER_NOTIFIED and ref.notified_at:
            age = now - _as_utc(ref.notified_at)
            if age > timedelta(hours=24):
                hours = int(age.total_seconds() // 3600)
                item.add(60, f"referral not yet confirmed after {hours} hours")
            else:
                item.add(20, "referral is in progress, keep an eye on it")
        else:
            item.add(30, "referral registered, caregiver not yet reminded")

        sign = ref.danger_sign.lower()
        if any(k in sign for k in HIGH_RISK_SIGNS):
            item.add(25, f"serious danger sign: {ref.danger_sign}")

        if household is not None and not household.caregiver_number:
            item.add(15, "this family has no phone, so they need a visit in person")

        items.append(item)

    items.sort(key=lambda i: i.score, reverse=True)
    return items[:limit]


def format_priority_message(items: list[PriorityItem]) -> str:
    if not items:
        return "No open referrals today. All your families are accounted for. Well done."
    lines = ["Good morning. Here are your priority visits for today:"]
    for rank, item in enumerate(items, start=1):
        reasons = "; ".join(item.reasons)
        lines.append(
            f"{rank}. {item.patient_name}, {item.community} (referral {item.referral_id})\n   Reason: {reasons}"
        )
    return "\n".join(lines)
