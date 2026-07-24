from decimal import Decimal, ROUND_HALF_UP
from .models import Quote

TWOPLACES = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    return Decimal(value or 0).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def percent(value: Decimal) -> Decimal:
    return Decimal(value or 0).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def quote_totals(quote: Quote) -> dict[str, Decimal | bool]:
    active_lines = [line for line in quote.lines if not line.is_section_header]
    revenue = sum((line.revenue for line in active_lines), Decimal("0.00"))
    cost = sum((line.cost for line in active_lines), Decimal("0.00"))
    gross_markup = revenue - cost
    gross_margin_percent = Decimal("0.00") if revenue == 0 else (gross_markup / revenue) * Decimal("100.00")
    markup_percent = Decimal("0.00") if cost == 0 else (gross_markup / cost) * Decimal("100.00")
    quoted_labor_hours = Decimal(quote.quoted_labor_hours or Decimal("0.00"))
    hourly_labor_rate = Decimal(quote.hourly_labor_rate or Decimal("0.00"))
    sph = Decimal("0.00") if quoted_labor_hours == 0 else (gross_markup / quoted_labor_hours) + hourly_labor_rate
    gross_markup_per_hour = Decimal("0.00") if quoted_labor_hours == 0 else gross_markup / quoted_labor_hours
    target_margin = Decimal(quote.target_margin_percent or Decimal("0.00"))

    return {
        "revenue": money(revenue),
        "cost": money(cost),
        "gross_profit": money(gross_markup),
        "gross_markup": money(gross_markup),
        "gross_margin_percent": percent(gross_margin_percent),
        "markup_percent": percent(markup_percent),
        "quoted_labor_hours": percent(quoted_labor_hours),
        "hourly_labor_rate": money(hourly_labor_rate),
        "gross_markup_per_hour": money(gross_markup_per_hour),
        "sph": money(sph),
        "labor_hours": percent(quoted_labor_hours),
        "profit_per_hour": money(gross_markup_per_hour),
        "sales_per_hour": Decimal("0.00") if quoted_labor_hours == 0 else money(revenue / quoted_labor_hours),
        "target_margin_percent": percent(target_margin),
        "passes_target_margin": gross_margin_percent >= target_margin,
    }


def price_for_markup(cost: Decimal, markup_percent: Decimal) -> Decimal:
    return money(Decimal(cost or 0) * (Decimal("1.00") + Decimal(markup_percent or 0) / Decimal("100.00")))


def price_for_target_margin(cost: Decimal, target_margin_percent: Decimal) -> Decimal:
    margin_decimal = Decimal(target_margin_percent or 0) / Decimal("100.00")
    if margin_decimal >= Decimal("1.00"):
        raise ValueError("Target margin must be less than 100%")
    return money(Decimal(cost or 0) / (Decimal("1.00") - margin_decimal))
