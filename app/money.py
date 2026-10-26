"""Money helpers. Everything financial is Decimal, rounded half-up to cents. Never float."""
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def money(x: Decimal) -> Decimal:
    """Quantize to cents using half-up (what accountants expect; Python's default is half-even)."""
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def line_total(quantity: Decimal, unit_price: Decimal) -> Decimal:
    # Round PER LINE so the printed lines always sum to the printed subtotal.
    return money(quantity * unit_price)


def compute_totals(lines: Iterable[tuple[Decimal, Decimal]], tax_rate_pct: Decimal):
    """lines: (quantity, unit_price) pairs. Returns (line_totals, subtotal, tax, total)."""
    totals = [line_total(q, p) for q, p in lines]
    subtotal = sum(totals, ZERO)
    tax = money(subtotal * tax_rate_pct / Decimal(100))
    return totals, subtotal, tax, subtotal + tax
