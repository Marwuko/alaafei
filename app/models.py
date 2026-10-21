"""Core entities for the Alaafei referral loop.

Deliberately minimal: this is the demo schema for the AI for Nurturing Care
bootcamp. Referral state is the heart of the system — everything else exists
to move a referral from REGISTERED to ARRIVED (or to escalate it loudly).
"""
import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ReferralStatus(str, enum.Enum):
    REGISTERED = "registered"        # nurse logged it
    CAREGIVER_NOTIFIED = "notified"  # voice/text reminder sent
    ARRIVED = "arrived"              # facility confirmed with one tap
    ESCALATED = "escalated"          # not confirmed in time -> nurse alerted
    CLOSED = "closed"                # nurse resolved after follow-up


class Nurse(Base):
    __tablename__ = "nurses"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    wa_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    chps_zone: Mapped[str] = mapped_column(String(120))

    referrals: Mapped[list["Referral"]] = relationship(back_populates="nurse")


class Facility(Base):
    __tablename__ = "facilities"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    wa_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    district: Mapped[str] = mapped_column(String(120))


class FacilityZone(Base):
    """Which facility a CHPS zone refers to. A zone has one receiving
    facility; a facility serves many zones. Zones with no row fall back to
    the lowest-id facility, which is what every referral did before."""
    __tablename__ = "facility_zones"

    chps_zone: Mapped[str] = mapped_column(String(120), primary_key=True)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"))


class Household(Base):
    __tablename__ = "households"

    id: Mapped[int] = mapped_column(primary_key=True)
    caregiver_name: Mapped[str] = mapped_column(String(120))
    caregiver_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    community: Mapped[str] = mapped_column(String(120))
    preferred_language: Mapped[str] = mapped_column(String(32), default="dagbani")
    # Households without a phone route through the nurse: number stays NULL
    # and every caregiver-facing message is delivered to the nurse instead.


class Referral(Base):
    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(primary_key=True)
    nurse_id: Mapped[int] = mapped_column(ForeignKey("nurses.id"))
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"))
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id"))

    patient_name: Mapped[str] = mapped_column(String(120))
    danger_sign: Mapped[str] = mapped_column(Text)
    status: Mapped[ReferralStatus] = mapped_column(
        Enum(ReferralStatus), default=ReferralStatus.REGISTERED, index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    arrived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Follow-ups after escalation: when the nurse was last reminded and how
    # many times, so the sweep can space them out and eventually stop.
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reminder_count: Mapped[int] = mapped_column(default=0, server_default="0")

    nurse: Mapped[Nurse] = relationship(back_populates="referrals")
    facility: Mapped[Facility] = relationship()
    household: Mapped[Household] = relationship()


class MediaClip(Base):
    """A WhatsApp media id for a clip in media/, with when it was uploaded.
    Meta drops uploads after about 30 days, so this is a cache, not config."""
    __tablename__ = "media_clips"

    clip: Mapped[str] = mapped_column(String(64), primary_key=True)
    media_id: Mapped[str] = mapped_column(String(64))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MessageLog(Base):
    __tablename__ = "message_log"
    # Every inbound WhatsApp message id lands here exactly once.
    # Duplicate webhook deliveries (WhatsApp retries aggressively on slow
    # responses) are detected by primary-key conflict and skipped: this is
    # what makes the whole pipeline idempotent.

    wa_message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    from_number: Mapped[str] = mapped_column(String(32), index=True)
    body: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
