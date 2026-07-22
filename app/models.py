from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, Text, Integer
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
    name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    fully_qualified_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    item_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    purchase_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class Quote(Base):
    __tablename__ = "quotes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    customer_qbo_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    customer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(64), default="Draft")
    target_margin_percent: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=Decimal("40.00"))
    qbo_estimate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qbo_estimate_doc_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    lines: Mapped[list["QuoteLine"]] = relationship(back_populates="quote", cascade="all, delete-orphan")


class QuoteLine(Base):
    __tablename__ = "quote_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    quote_id: Mapped[int] = mapped_column(ForeignKey("quotes.id"), nullable=False)
    line_type: Mapped[str] = mapped_column(String(64), default="Material")
    description: Mapped[str] = mapped_column(Text, nullable=False)
    qbo_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qbo_item_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("1.00"))
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    labor_hours: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    include_on_qbo_estimate: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    quote: Mapped[Quote] = relationship(back_populates="lines")

    @property
    def revenue(self) -> Decimal:
        return (self.quantity or Decimal("0.00")) * (self.unit_price or Decimal("0.00"))

    @property
    def cost(self) -> Decimal:
        return (self.quantity or Decimal("0.00")) * (self.unit_cost or Decimal("0.00"))

    @property
    def gross_profit(self) -> Decimal:
        return self.revenue - self.cost

    @property
    def markup_percent(self) -> Decimal | None:
        if self.cost == 0:
            return None
        return ((self.gross_profit / self.cost) * Decimal("100.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
