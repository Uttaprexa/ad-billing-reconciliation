"""Pure-logic tests: money rounding, CSV parsing, reconciliation, aging. No DB needed."""
from datetime import date
from decimal import Decimal as D

import pytest

from app.money import money, compute_totals, line_total
from app.reconcile import (InvoiceState, Status, parse_payments_csv, reconcile, aging, CsvFormatError)


# ---------- money / rounding ----------
def test_float_trap_avoided():
    assert D("0.1") + D("0.2") == D("0.3")           # 0.1 + 0.2 != 0.3 with floats

def test_half_up_not_bankers():
    assert money(D("2.675")) == D("2.68")             # round() on floats gives 2.67
    assert money(D("0.125")) == D("0.13")             # half-even would give 0.12

def test_line_rounding_per_line():
    # three lines of 0.333 x 1 => each rounds to 0.33, subtotal 0.99 (not 1.00)
    totals, sub, tax, total = compute_totals([(D("1"), D("0.333"))] * 3, D("0"))
    assert totals == [D("0.33")] * 3 and sub == D("0.99") and total == D("0.99")

def test_tax_rounding():
    _, sub, tax, total = compute_totals([(D("3"), D("19.99"))], D("8.875"))
    assert sub == D("59.97") and tax == D("5.32") and total == D("65.29")   # 5.3223 -> 5.32

def test_fractional_quantity():
    assert line_total(D("1.5"), D("0.01")) == D("0.02")   # 0.015 -> 0.02

def test_zero_tax_and_empty():
    assert compute_totals([], D("10"))[1:] == (D("0.00"), D("0.00"), D("0.00"))


# ---------- csv parsing ----------
def csv_of(*lines):
    return "payment_id,reference,amount,date\n" + "\n".join(lines)

def test_csv_missing_column():
    with pytest.raises(CsvFormatError):
        parse_payments_csv("payment_id,amount\np1,5")

@pytest.mark.parametrize("bad", ["NaN", "Infinity", "1e5", "10.005", "abc", "", "$5"])
def test_csv_rejects_weird_amounts(bad):
    rows = parse_payments_csv(csv_of(f"p1,INV-000001,{bad},2025-01-01"))
    assert rows[0].error and "amount" in rows[0].error

def test_csv_extra_column_is_an_error_not_a_crash():
    r = parse_payments_csv(csv_of("p1,INV-000001,1,000.00,2025-01-01"))[0]
    assert r.error and "too many columns" in r.error


def test_csv_bad_date_and_missing_id():
    r = parse_payments_csv(csv_of(",INV-000001,5.00,31/12/2025"))[0]
    assert "date" in r.error and "payment_id" in r.error


# ---------- reconciliation ----------
def inv(n, bal, status="APPROVED", issued=date(2025, 1, 1)):
    return InvoiceState(n, status, issued, D(bal))

INVS = {"INV-000001": inv("INV-000001", "100.00"), "INV-000002": inv("INV-000002", "50.00"),
        "INV-000003": inv("INV-000003", "80.00", status="DRAFT")}

def run(*lines, invoices=INVS, **kw):
    return reconcile(invoices, parse_payments_csv(csv_of(*lines)), **kw)

def test_matched():
    r = run("p1,Payment INV-000001,100.00,2025-02-01")[0]
    assert r.status == Status.MATCHED and r.balance_after == D("0.00")

def test_partial_then_remainder_matches():
    a, b = run("p1,INV-000001,40.00,2025-02-01", "p2,INV-000001,60.00,2025-02-02")
    assert a.status == Status.PARTIAL and a.balance_after == D("60.00")
    assert b.status == Status.MATCHED and b.balance_after == D("0.00")

def test_near_match_flag():
    r = run("p1,INV-000001,99.50,2025-02-01")[0]
    assert r.status == Status.PARTIAL and "NEAR_MATCH_SHORT" in r.flags

def test_overpayment_flagged():
    r = run("p1,INV-000001,120.00,2025-02-01")[0]
    assert r.status == Status.OVERPAID and "OVERPAYMENT" in r.flags and r.balance_after == D("-20.00")

def test_duplicate_payment_id():
    a, b = run("p1,INV-000001,40.00,2025-02-01", "p1,INV-000001,40.00,2025-02-01")
    assert a.status == Status.PARTIAL and b.status == Status.DUPLICATE
    assert b.balance_after is None            # duplicate must NOT change any balance

def test_duplicate_same_invoice_amount_date_different_id():
    a, b = run("p1,INV-000001,40.00,2025-02-01", "p2,INV-000001,40.00,2025-02-01")
    assert b.status == Status.DUPLICATE and "SAME_INVOICE_AMOUNT_DATE" in b.flags

def test_duplicate_against_already_stored_payment():
    r = run("p9,INV-000001,10.00,2025-02-01", known_payment_ids=frozenset({"p9"}))[0]
    assert r.status == Status.DUPLICATE

def test_negative_and_zero_amounts_invalid():
    neg, zero = run("p1,INV-000001,-25.00,2025-02-01", "p2,INV-000001,0.00,2025-02-01")
    assert neg.status == Status.INVALID and "NEGATIVE_AMOUNT" in neg.flags
    assert zero.status == Status.INVALID

def test_unmatched_variants():
    no_ref, unknown, draft, multi = run(
        "p1,wire transfer,10.00,2025-02-01", "p2,INV-999999,10.00,2025-02-01",
        "p3,INV-000003,10.00,2025-02-01", "p4,INV-000001 INV-000002,10.00,2025-02-01")
    assert [x.status for x in (no_ref, unknown, draft, multi)] == [Status.UNMATCHED] * 4
    assert "NO_REFERENCE" in no_ref.flags and "UNKNOWN_INVOICE" in unknown.flags
    assert "INVOICE_NOT_APPROVED" in draft.flags and "AMBIGUOUS_REFERENCE" in multi.flags

def test_reference_case_insensitive():
    assert run("p1,paid inv-000002 thanks,50.00,2025-02-01")[0].status == Status.MATCHED

def test_payment_before_issue_flag():
    r = run("p1,INV-000001,10.00,2024-12-01")[0]
    assert "PAYMENT_BEFORE_ISSUE" in r.flags

def test_invalid_row_does_not_stop_processing():
    bad, good = run("p1,INV-000001,abc,2025-02-01", "p2,INV-000001,100.00,2025-02-01")
    assert bad.status == Status.INVALID and good.status == Status.MATCHED


# ---------- aging ----------
def test_aging_buckets_and_boundaries():
    today = date(2025, 6, 30)
    items = [(date(2025, 7, 1), D("10")),   # not due yet -> current
             (date(2025, 6, 30), D("20")),  # due today  -> current
             (date(2025, 5, 31), D("30")),  # 30 days    -> 1-30
             (date(2025, 5, 30), D("40")),  # 31 days    -> 31-60
             (date(2025, 1, 1), D("50")),   # 90+
             (date(2025, 1, 1), D("-5"))]   # credit balance ignored
    b = aging(items, today)
    assert b == {"current": D("30"), "1-30": D("30"), "31-60": D("40"), "61-90": D("0.00"), "90+": D("50")}
