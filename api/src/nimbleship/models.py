from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nimbleship.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class Consignment(Base):
    __tablename__ = "consignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_number: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    recipient_name: Mapped[str] = mapped_column(String(255))
    address_lines: Mapped[list[str]] = mapped_column(JSON)
    postcode: Mapped[str] = mapped_column(String(32))
    destination_country: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(32))
    carrier: Mapped[str | None] = mapped_column(String(64), nullable=True)
    service: Mapped[str | None] = mapped_column(String(64), nullable=True)
    allocation: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    parcels: Mapped[list["Parcel"]] = relationship(
        back_populates="consignment", order_by="Parcel.sequence"
    )


class Parcel(Base):
    __tablename__ = "parcels"
    __table_args__ = (UniqueConstraint("consignment_id", "sequence"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    consignment_id: Mapped[int] = mapped_column(ForeignKey("consignments.id"))
    sequence: Mapped[int] = mapped_column()
    weight_kg: Mapped[str] = mapped_column(String(16))
    barcode: Mapped[str] = mapped_column(String(80))

    consignment: Mapped[Consignment] = relationship(back_populates="parcels")


class OrderEvent(Base):
    """Append-only order timeline: the spine of the system (ROADMAP Phase 1).

    Rows are only ever inserted, never updated or deleted."""

    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_number: Mapped[str] = mapped_column(String(64), index=True)
    stage: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DeliveryProposition(Base):
    """The Delivery Proposition catalogue (CONTEXT.md): the customer-facing
    delivery promises services may declare they fulfil. The code is the
    natural key rulebook declarations reference."""

    __tablename__ = "delivery_propositions"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(String(500))


class RulebookVersion(Base):
    """A versioned rulebook per ADR 0003: immutable rows, draft or published;
    the highest published version is live."""

    __tablename__ = "rulebook_versions"

    version: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(16))
    author: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
