from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, String, Text, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base


class QboConnection(Base):
    __tablename__ = "qbo_connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    realm_id: Mapped[str] = mapped_column(String(64), nullable=False)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class QboCustomer(Base):
    __tablename__ = "qbo_customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    qbo_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class QboItem(Base):
    __tablename__ = "qbo_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    qbo_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    sync_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    fully_qualified_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sku: Mapped[str | None] = mapped_column(String(255), nullable=True)
    item_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    purchase_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    original_unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    original_purchase_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    qty_on_hand: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    variable_cost: Mapped[bool] = mapped_column(Boolean, default=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    @property
    def markup_percent(self) -> Decimal | None:
        if not self.purchase_cost:
            return None
        return (((self.unit_price or Decimal("0.00")) - self.purchase_cost) / self.purchase_cost * Decimal("100.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def is_changed(self) -> bool:
        return (self.unit_price or Decimal("0.00")) != (self.original_unit_price or Decimal("0.00")) or (self.purchase_cost or Decimal("0.00")) != (self.original_purchase_cost or Decimal("0.00"))


class Quote(Base):
    __tablename__ = "quotes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    customer_qbo_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    customer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(64), default="Draft")
    target_margin_percent: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=Decimal("40.00"))
    quoted_labor_hours: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    hourly_labor_rate: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    qbo_estimate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qbo_estimate_doc_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qbo_sync_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qbo_txn_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    qbo_total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    qbo_last_updated_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sph_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_interacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    lines: Mapped[list["QuoteLine"]] = relationship(back_populates="quote", cascade="all, delete-orphan")


class QuoteLine(Base):
    __tablename__ = "quote_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    quote_id: Mapped[int] = mapped_column(ForeignKey("quotes.id"), nullable=False)
    line_type: Mapped[str] = mapped_column(String(64), default="Material")
    description: Mapped[str] = mapped_column(Text, nullable=False)
    qbo_line_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qbo_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qbo_item_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product_service_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("1.00"))
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    labor_hours: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    include_on_qbo_estimate: Mapped[bool] = mapped_column(Boolean, default=True)
    is_section_header: Mapped[bool] = mapped_column(Boolean, default=False)
    is_variable_cost: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    quote: Mapped[Quote] = relationship(back_populates="lines")

    @property
    def revenue(self) -> Decimal:
        if self.is_section_header:
            return Decimal("0.00")
        return (self.quantity or Decimal("0.00")) * (self.unit_price or Decimal("0.00"))

    @property
    def cost(self) -> Decimal:
        if self.is_section_header:
            return Decimal("0.00")
        return (self.quantity or Decimal("0.00")) * (self.unit_cost or Decimal("0.00"))

    @property
    def gross_profit(self) -> Decimal:
        return self.revenue - self.cost

    @property
    def markup_percent(self) -> Decimal | None:
        if self.is_section_header or not self.unit_cost:
            return None
        return (((self.unit_price or Decimal("0.00")) - self.unit_cost) / self.unit_cost * Decimal("100.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
