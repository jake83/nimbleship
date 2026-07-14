from datetime import UTC, datetime
from datetime import date as date_type

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
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
    # The Delivery Proposition the customer bought; kept so dry-run replays
    # evaluate the same facts dispatch saw (ADR 0003/0007).
    proposition: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    carrier: Mapped[str | None] = mapped_column(String(64), nullable=True)
    service: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The Warehouse code the consignment dispatches from (CONTEXT.md:
    # Warehouse). A denormalised copy like carrier/service: the allocation
    # record must survive later warehouse edits.
    warehouse: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The carrier's own reference for the consignment, extracted from the
    # book operation's response. None until a live-API carrier has booked.
    tracking_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
    # The barcode the carrier issued for this parcel at booking - distinct
    # from `barcode`, the Parcel Barcode this system prints (CONTEXT.md).
    carrier_barcode: Mapped[str | None] = mapped_column(String(80), nullable=True)

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


class Warehouse(Base):
    """A logical dispatch identity (CONTEXT.md: Warehouse) - the sender the
    WMS names per order, not necessarily a physical building. Carries
    collection days and holidays."""

    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    # Sender details for labels and carrier bookings.
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address_lines: Mapped[list[str]] = mapped_column(JSON)
    postcode: Mapped[str] = mapped_column(String(32))
    country: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    # One row per warehouse; None only transiently, before first flush.
    collection_days: Mapped["WarehouseCollectionDay | None"] = relationship(
        back_populates="warehouse", cascade="all, delete-orphan"
    )
    holidays: Mapped[list["WarehouseHoliday"]] = relationship(
        back_populates="warehouse",
        cascade="all, delete-orphan",
        order_by="WarehouseHoliday.date",
    )


class WarehouseCollectionDay(Base):
    """Weekday collection flags, one row per warehouse: the flags are
    global, not per-carrier."""

    __tablename__ = "warehouse_collection_days"

    id: Mapped[int] = mapped_column(primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), unique=True)
    monday: Mapped[bool] = mapped_column(default=True)
    tuesday: Mapped[bool] = mapped_column(default=True)
    wednesday: Mapped[bool] = mapped_column(default=True)
    thursday: Mapped[bool] = mapped_column(default=True)
    friday: Mapped[bool] = mapped_column(default=True)
    saturday: Mapped[bool] = mapped_column(default=False)
    sunday: Mapped[bool] = mapped_column(default=False)

    warehouse: Mapped[Warehouse] = relationship(back_populates="collection_days")


class WarehouseHoliday(Base):
    """A date the warehouse does not dispatch (bank holiday, shutdown)."""

    __tablename__ = "warehouse_holidays"
    __table_args__ = (UniqueConstraint("warehouse_id", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))
    date: Mapped[date_type] = mapped_column(Date, index=True)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)

    warehouse: Mapped[Warehouse] = relationship(back_populates="holidays")


class ShippingArea(Base):
    """A named geography defined by postcode prefixes (CONTEXT.md: Shipping
    Area). Services reference areas by code in their declarations; the
    mechanism is data, never constants."""

    __tablename__ = "shipping_areas"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    country: Mapped[str] = mapped_column(String(3))

    prefixes: Mapped[list["PostcodeArea"]] = relationship(
        back_populates="area",
        order_by="PostcodeArea.prefix",
        cascade="all, delete-orphan",
    )


class PostcodeArea(Base):
    """One postcode prefix defining part of a Shipping Area. Prefixes are
    stored normalised (uppercase, trimmed); the resolver matches a
    destination postcode to areas by its longest matching prefix."""

    __tablename__ = "postcode_areas"
    __table_args__ = (UniqueConstraint("area_id", "prefix"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    area_id: Mapped[int] = mapped_column(ForeignKey("shipping_areas.id"))
    prefix: Mapped[str] = mapped_column(String(16), index=True)

    area: Mapped[ShippingArea] = relationship(back_populates="prefixes")


class RulebookVersion(Base):
    """A versioned rulebook per ADR 0003: immutable rows, draft or published;
    the highest published version is live."""

    __tablename__ = "rulebook_versions"

    version: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(16))
    author: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CarrierDefinitionVersion(Base):
    """A versioned Carrier Definition per carrier (ADR 0009 on the ADR 0003
    rails): immutable rows, draft or published; the highest published
    version per carrier is live."""

    __tablename__ = "carrier_definition_versions"
    __table_args__ = (UniqueConstraint("carrier", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    carrier: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column()
    status: Mapped[str] = mapped_column(String(16))
    author: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CarrierNumberSequence(Base):
    """A named sequential number range per carrier, for carriers that make
    the client mint consignment identifiers. Claimed only through
    nimbleship.engine.plugins.number_range.allocate_number, which guards
    against double allocation - never read-and-bumped directly."""

    __tablename__ = "carrier_number_sequences"

    carrier: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    next_value: Mapped[int] = mapped_column(default=1)
    # The exhaustion policy fixed at creation ("wrap" or "halt"): stored so an
    # exhausted halt range cannot be reissued by a later wrap allocation.
    # Nullable for rows created before the column existed; they backfill on
    # their next allocation.
    policy: Mapped[str | None] = mapped_column(String(8), nullable=True)


class CarrierTraffic(Base):
    """One executed carrier step: the rendered request and the raw
    response. Append-only - this is the golden corpus Golden Replay diffs
    draft definitions against (ADR 0009). Response bodies arrive truncated
    to the executor's TRAFFIC_BODY_LIMIT."""

    __tablename__ = "carrier_traffic"

    id: Mapped[int] = mapped_column(primary_key=True)
    carrier: Mapped[str] = mapped_column(String(64), index=True)
    order_number: Mapped[str] = mapped_column(String(64), index=True)
    step: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict[str, object]] = mapped_column(JSON)
    # None when the carrier was never reached (connect/timeout failures).
    response_status: Mapped[int | None] = mapped_column(nullable=True)
    response_body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Manifest(Base):
    """The per-carrier declaration of consignments that have physically left
    the warehouse (CONTEXT.md: Manifest), created when the WMS confirms
    dispatch and sent to the carrier by a queue worker with retries
    (ADR 0004). One manifest per carrier and warehouse per confirmation."""

    __tablename__ = "manifests"

    id: Mapped[int] = mapped_column(primary_key=True)
    carrier: Mapped[str] = mapped_column(String(64), index=True)
    # The Warehouse code the manifested consignments dispatched from; a
    # denormalised copy, like Consignment.warehouse.
    warehouse: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # pending -> sent, or -> failed once the send job exhausts its retries.
    status: Mapped[str] = mapped_column(String(16))
    # Send attempts so far - the queue owns scheduling; this is the audit.
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ManifestConsignment(Base):
    """One consignment declared on one Manifest."""

    __tablename__ = "manifest_consignments"
    __table_args__ = (UniqueConstraint("manifest_id", "consignment_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    manifest_id: Mapped[int] = mapped_column(ForeignKey("manifests.id"), index=True)
    consignment_id: Mapped[int] = mapped_column(ForeignKey("consignments.id"))


class CarrierConfig(Base):
    """Per-install carrier account facts (credentials, endpoints, account
    numbers) referenced by definitions as config.* sources. Never part of a
    definition - a fresh install is a deploy plus configuration."""

    __tablename__ = "carrier_configs"

    carrier: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[dict[str, object]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
