"""Pure ad-delivery logic: CPM billing, shortfall credits, delivery-row classification, anomaly rules.
No DB, no web, so every rule is unit-testable.

CPM = cost per mille = price per 1,000 impressions.  amount = impressions / 1000 * CPM  (Decimal, half-up cents).
"""
import csv
import io
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from statistics import median

from .money import money, ZERO


def impressions_value(impressions: int, cpm: Decimal) -> Decimal:
    return money(Decimal(impressions) * cpm / Decimal(1000))


def credit_for_shortfall(booked: int, delivered: int, cpm: Decimal, tax_rate_pct: Decimal) -> Decimal:
    """Makegood credit for under-delivery, including the tax that was billed on the undelivered part."""
    short = booked - delivered
    if short <= 0:
        return ZERO
    pre_tax = impressions_value(short, cpm)
    return money(pre_tax * (Decimal(100) + tax_rate_pct) / Decimal(100))


class DStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"              # same campaign+day+impressions already stored: safe to ignore
    CONFLICT = "CONFLICT"                # same campaign+day but DIFFERENT impressions: needs a human
    UNKNOWN_CAMPAIGN = "UNKNOWN_CAMPAIGN"
    OUT_OF_FLIGHT = "OUT_OF_FLIGHT"
    INVALID = "INVALID"


@dataclass(frozen=True)
class DeliveryRow:
    line: int
    campaign: str
    day: date | None
    impressions: int | None
    error: str | None = None


@dataclass
class DeliveryResult:
    line: int
    campaign: str
    day: date | None
    impressions: int | None
    status: DStatus
    message: str = ""


@dataclass(frozen=True)
class FlightInfo:
    code: str
    start: date
    end: date


class DeliveryCsvError(ValueError):
    pass


_REQUIRED = {"campaign", "date", "impressions"}
_INT_RE = re.compile(r"^\d{1,12}$")   # no negatives, decimals, 1e6, or thousands separators


def parse_delivery_csv(text: str) -> list[DeliveryRow]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise DeliveryCsvError("CSV is empty")
    missing = _REQUIRED - {c.strip().lower() for c in reader.fieldnames if c}
    if missing:
        raise DeliveryCsvError(f"CSV is missing required columns: {', '.join(sorted(missing))}")
    rows = []
    for i, raw in enumerate(reader, start=2):
        extra = raw.pop(None, None)   # DictReader stores surplus cells under key None (e.g. an unquoted "1,000")
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        err, day, impr = None, None, None
        if extra:
            err = "too many columns in this row (is a number like 1,000 missing quotes?)"
        elif not _INT_RE.match(r["impressions"]):
            err = f"invalid impressions '{r['impressions']}' (whole number >= 0)"
        else:
            impr = int(r["impressions"])
        try:
            day = date.fromisoformat(r["date"])
        except ValueError:
            err = (err + "; " if err else "") + f"invalid date '{r['date']}' (use YYYY-MM-DD)"
        if not r["campaign"]:
            err = (err + "; " if err else "") + "missing campaign"
        rows.append(DeliveryRow(i, r["campaign"].upper(), day, impr, err))
    return rows


def classify_rows(flights: dict[str, FlightInfo], existing: dict[tuple[str, date], int],
                  rows: list[DeliveryRow]) -> list[DeliveryResult]:
    seen = dict(existing)
    out = []
    for r in rows:
        def res(status, msg=""):
            return DeliveryResult(r.line, r.campaign, r.day, r.impressions, status, msg)
        if r.error:
            out.append(res(DStatus.INVALID, r.error)); continue
        f = flights.get(r.campaign)
        if f is None:
            out.append(res(DStatus.UNKNOWN_CAMPAIGN, "no such campaign")); continue
        if not (f.start <= r.day <= f.end):
            out.append(res(DStatus.OUT_OF_FLIGHT, f"outside flight {f.start} to {f.end}")); continue
        key = (r.campaign, r.day)
        if key in seen:
            if seen[key] == r.impressions:
                out.append(res(DStatus.DUPLICATE, "already recorded"))
            else:
                out.append(res(DStatus.CONFLICT, f"already recorded as {seen[key]:,}; not overwritten"))
            continue
        seen[key] = r.impressions
        out.append(res(DStatus.ACCEPTED))
    return out


def delivery_anomalies(series: dict[date, int], spike_factor: int = 3) -> list[tuple[date, str]]:
    """Simple, explainable rules on one campaign's daily impressions.
    MISSING_DAY    a gap between the first and last reported day
    ZERO_DELIVERY  a reported day with 0 impressions
    SPIKE          a day above spike_factor x the median of the OTHER days (needs >= 4 other days)
    """
    flags: list[tuple[date, str]] = []
    if not series:
        return flags
    days = sorted(series)
    d = days[0]
    while d <= days[-1]:
        if d not in series:
            flags.append((d, "MISSING_DAY"))
        d += timedelta(days=1)
    for day, v in series.items():
        if v == 0:
            flags.append((day, "ZERO_DELIVERY"))
        others = [x for k, x in series.items() if k != day]
        if len(others) >= 4:
            m = median(others)
            if m > 0 and v > spike_factor * m:
                flags.append((day, "SPIKE"))
    return sorted(flags)
