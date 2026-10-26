"""Pure reconciliation logic (no DB, no web) so it is trivial to unit test.

Statuses per payment row:
  MATCHED    amount == invoice balance
  PARTIAL    0 < amount < balance
  OVERPAID   amount > balance (customer credit; flagged)
  UNMATCHED  no / unknown / ambiguous invoice reference, or invoice not approved
  DUPLICATE  same payment_id, or same invoice+amount+date, already seen
  INVALID    unparsable, zero or negative amount, bad date
"""
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum

from .money import money, ZERO


class Status(str, Enum):
    MATCHED = "MATCHED"
    PARTIAL = "PARTIAL"
    OVERPAID = "OVERPAID"
    UNMATCHED = "UNMATCHED"
    DUPLICATE = "DUPLICATE"
    INVALID = "INVALID"


@dataclass(frozen=True)
class InvoiceState:
    number: str
    status: str          # DRAFT / PENDING / APPROVED / REJECTED
    issue_date: date
    balance: Decimal     # total - approved credits - payments already applied


@dataclass(frozen=True)
class PaymentRow:
    line: int
    payment_id: str
    reference: str
    amount: Decimal | None
    paid_on: date | None
    error: str | None = None


@dataclass
class Result:
    line: int
    payment_id: str
    reference: str
    amount: Decimal | None
    paid_on: date | None
    status: Status
    invoice_number: str | None = None
    balance_after: Decimal | None = None
    flags: list[str] = field(default_factory=list)
    message: str = ""


class CsvFormatError(ValueError):
    pass


REQUIRED_COLUMNS = {"payment_id", "reference", "amount", "date"}
_AMOUNT_RE = re.compile(r"^-?\d+(\.\d{1,2})?$")
_INV_RE = re.compile(r"INV-\d+", re.IGNORECASE)


def parse_payments_csv(text: str) -> list[PaymentRow]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise CsvFormatError("CSV is empty")
    cols = {c.strip().lower() for c in reader.fieldnames if c}
    missing = REQUIRED_COLUMNS - cols
    if missing:
        raise CsvFormatError(f"CSV is missing required columns: {', '.join(sorted(missing))}")
    rows = []
    for i, raw in enumerate(reader, start=2):  # line 1 is the header
        extra = raw.pop(None, None)   # DictReader stores surplus cells under key None (e.g. an unquoted "1,000.00")
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        error = None
        amount = paid_on = None
        # Strict regex: rejects 'NaN', 'Infinity', '1e5', '10.005' which Decimal() would happily accept.
        if extra:
            error = "too many columns in this row (is an amount like 1,000.00 missing quotes?)"
        elif not _AMOUNT_RE.match(r["amount"]):
            error = f"invalid amount '{r['amount']}'"
        else:
            amount = Decimal(r["amount"])
        try:
            paid_on = date.fromisoformat(r["date"])
        except ValueError:
            error = (error + "; " if error else "") + f"invalid date '{r['date']}' (use YYYY-MM-DD)"
        if not r["payment_id"]:
            error = (error + "; " if error else "") + "missing payment_id"
        rows.append(PaymentRow(i, r["payment_id"], r["reference"], amount, paid_on, error))
    return rows


def reconcile(
    invoices: dict[str, InvoiceState],
    rows: list[PaymentRow],
    known_payment_ids: frozenset[str] = frozenset(),
    known_keys: frozenset[tuple] = frozenset(),
    near_match_tolerance: Decimal = Decimal("1.00"),
) -> list[Result]:
    balances = {n: s.balance for n, s in invoices.items()}
    seen_ids = set(known_payment_ids)
    seen_keys = set(known_keys)  # (invoice_number, amount, date)
    out: list[Result] = []

    for row in rows:
        def res(status, **kw):
            return Result(row.line, row.payment_id, row.reference, row.amount, row.paid_on, status, **kw)

        # 1. invalid rows
        if row.error:
            out.append(res(Status.INVALID, message=row.error)); continue
        if row.amount <= ZERO:
            flag = "NEGATIVE_AMOUNT" if row.amount < ZERO else "ZERO_AMOUNT"
            out.append(res(Status.INVALID, flags=[flag], message="amount must be positive (refunds are not handled here)"))
            continue

        refs = {m.upper() for m in _INV_RE.findall(row.reference)}
        ref = next(iter(refs)) if len(refs) == 1 else None

        # 2. duplicates (before matching, so they never touch a balance)
        if row.payment_id in seen_ids:
            out.append(res(Status.DUPLICATE, invoice_number=ref, flags=["SAME_PAYMENT_ID"],
                           message="payment_id already processed")); continue
        key = (ref, row.amount, row.paid_on)
        if ref and key in seen_keys:
            out.append(res(Status.DUPLICATE, invoice_number=ref, flags=["SAME_INVOICE_AMOUNT_DATE"],
                           message="same invoice, amount and date already seen: possible double posting")); continue

        # 3. find the invoice
        seen_ids.add(row.payment_id)
        if not refs:
            out.append(res(Status.UNMATCHED, flags=["NO_REFERENCE"], message="no invoice number in reference")); continue
        if ref is None:
            out.append(res(Status.UNMATCHED, flags=["AMBIGUOUS_REFERENCE"],
                           message=f"reference contains several invoices: {', '.join(sorted(refs))}")); continue
        inv = invoices.get(ref)
        if inv is None:
            out.append(res(Status.UNMATCHED, invoice_number=ref, flags=["UNKNOWN_INVOICE"], message="invoice not found")); continue
        if inv.status != "APPROVED":
            out.append(res(Status.UNMATCHED, invoice_number=ref, flags=["INVOICE_NOT_APPROVED"],
                           message=f"invoice is {inv.status}, not APPROVED")); continue

        # 4. classify against the running balance
        seen_keys.add(key)
        bal = balances[ref]
        flags: list[str] = []
        if row.paid_on < inv.issue_date:
            flags.append("PAYMENT_BEFORE_ISSUE")
        if row.amount == bal:
            status, msg = Status.MATCHED, "paid in full"
        elif row.amount < bal:
            status, msg = Status.PARTIAL, f"short by {money(bal - row.amount)}"
            if bal - row.amount <= near_match_tolerance:
                flags.append("NEAR_MATCH_SHORT")  # likely bank fee / rounding
        else:
            status, msg = Status.OVERPAID, f"over by {money(row.amount - bal)}"
            flags.append("OVERPAYMENT")
        balances[ref] = bal - row.amount
        out.append(res(status, invoice_number=ref, balance_after=balances[ref], flags=flags, message=msg))
    return out


def aging(items: list[tuple[date, Decimal]], as_of: date) -> dict[str, Decimal]:
    """items: (due_date, outstanding balance). Only positive balances are receivables."""
    buckets = {"current": ZERO, "1-30": ZERO, "31-60": ZERO, "61-90": ZERO, "90+": ZERO}
    for due, bal in items:
        if bal <= ZERO:
            continue
        days = (as_of - due).days
        if days <= 0: k = "current"
        elif days <= 30: k = "1-30"
        elif days <= 60: k = "31-60"
        elif days <= 90: k = "61-90"
        else: k = "90+"
        buckets[k] += bal
    return buckets
