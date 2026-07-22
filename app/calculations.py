from decimal import Decimal, ROUND_HALF_UP
from .models import Quote

TWOPLACES = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    return Decimal(value or 0).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def percent(value: Decimal) -> Decimal:
    return Decimal(value or 0).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def quote_totals(quote: Quote) -> dict[str, Decimal | bool]:
    revenue = sum((line.revenue for line in quote.lines), Decimal("0.00"))
    cost = sum((line.cost for line in quote.lines), Decimal("0.00"))
    labor_hours = sum((line.labor_hours or Decimal("0.00") for line in quote.lines), Decimal("0.00"))
    gross_profit = revenue - cost
    gross_margin_percent = Decimal("0.00") if revenue == 0 else (gross_profit / revenue) * Decimal("100.00")
    profit_per_hour = Decimal("0.00") if labor_hours == 0 else gross_profit / labor_hours
    sales_per_hour = Decimal("0.00") if labor_hours == 0 else revenue / labor_hours
    target_margin = Decimal(quote.target_margin_percent or Decimal("0.00"))

    return {
        "revenue": money(revenue),
        "cost": money(cost),
        "gross_profit": money(gross_profit),
        "gross_margin_percent": percent(gross_margin_percent),
        "labor_hours": percent(labor_hours),
        "profit_per_hour": money(profit_per_hour),
        "sales_per_hour": money(sales_per_hour),
        "target_margin_percent": percent(target_margin),
        "passes_target_margin": gross_margin_percent >= target_margin,
    }


def price_for_target_margin(cost: Decimal, target_margin_percent: Decimal) -> Decimal:
    margin_decimal = Decimal(target_margin_percent or 0) / Decimal("100.00")
    if margin_decimal >= Decimal("1.00"):
        raise ValueError("Target margin must be less than 100%")
    return money(Decimal(cost or 0) / (Decimal("1.00") - margin_decimal))
