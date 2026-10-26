"""Pure tests for CPM billing, shortfall credits, delivery CSV rows and anomaly rules."""
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from app.delivery import (DStatus, DeliveryCsvError, FlightInfo, classify_rows, credit_for_shortfall,
                          delivery_anomalies, impressions_value, parse_delivery_csv)


# ---------- CPM math ----------
def test_cpm_basic():
    assert impressions_value(1_000_000, D("12.50")) == D("12500.00")

def test_cpm_rounds_half_up():
    assert impressions_value(1, D("12.50")) == D("0.01")      # 0.0125 -> 0.01
    assert impressions_value(500, D("0.01")) == D("0.01")     # 0.005  -> 0.01 (half-even would give 0.00)

def test_cpm_zero_impressions():
    assert impressions_value(0, D("9.99")) == D("0.00")

def test_shortfall_credit_includes_tax():
    # 200,000 short @ $12.50 CPM = 2,500.00 pre-tax; 10% tax was billed on it -> 2,750.00
    assert credit_for_shortfall(1_000_000, 800_000, D("12.50"), D("10")) == D("2750.00")

def test_no_credit_when_fully_or_over_delivered():
    assert credit_for_shortfall(1_000_000, 1_000_000, D("12.50"), D("10")) == D("0.00")
    assert credit_for_shortfall(1_000_000, 1_500_000, D("12.50"), D("10")) == D("0.00")


# ---------- csv ----------
def csv_of(*lines):
    return "campaign,date,impressions\n" + "\n".join(lines)

def test_delivery_csv_missing_column():
    with pytest.raises(DeliveryCsvError):
        parse_delivery_csv("campaign,date\nA,2026-01-01")

@pytest.mark.parametrize("bad", ["-5", "1.5", "1e6", "1,000", "abc", ""])
def test_delivery_csv_rejects_bad_impressions(bad):
    assert parse_delivery_csv(csv_of(f"abc-1,2026-09-01,{bad}"))[0].error

def test_delivery_csv_uppercases_campaign_code():
    assert parse_delivery_csv(csv_of("abc-1,2026-09-01,10"))[0].campaign == "ABC-1"


# ---------- row classification ----------
FLIGHTS = {"ABC-1": FlightInfo("ABC-1", date(2026, 9, 1), date(2026, 9, 5))}

def classify(*lines, existing=None):
    return classify_rows(FLIGHTS, existing or {}, parse_delivery_csv(csv_of(*lines)))

def test_accepted_and_unknown_and_out_of_flight():
    ok, unk, late = classify("abc-1,2026-09-01,100", "zzz-9,2026-09-01,100", "abc-1,2026-09-06,100")
    assert (ok.status, unk.status, late.status) == (DStatus.ACCEPTED, DStatus.UNKNOWN_CAMPAIGN, DStatus.OUT_OF_FLIGHT)

def test_flight_boundaries_inclusive():
    a, b = classify("abc-1,2026-09-01,1", "abc-1,2026-09-05,1")
    assert a.status == b.status == DStatus.ACCEPTED

def test_duplicate_vs_conflict_in_same_file():
    first, same, different = classify("abc-1,2026-09-01,100", "abc-1,2026-09-01,100", "abc-1,2026-09-01,999")
    assert first.status == DStatus.ACCEPTED and same.status == DStatus.DUPLICATE and different.status == DStatus.CONFLICT

def test_stored_day_never_overwritten():
    r = classify("abc-1,2026-09-02,555", existing={("ABC-1", date(2026, 9, 2)): 100})[0]
    assert r.status == DStatus.CONFLICT and "100" in r.message

def test_bad_row_does_not_stop_the_rest():
    bad, good = classify("abc-1,2026-09-01,abc", "abc-1,2026-09-02,10")
    assert bad.status == DStatus.INVALID and good.status == DStatus.ACCEPTED


# ---------- anomaly rules ----------
def series(*vals, start=date(2026, 9, 1)):
    return {start + timedelta(days=i): v for i, v in enumerate(vals) if v is not None}

def test_clean_series_has_no_flags():
    assert delivery_anomalies(series(100, 110, 95, 105, 100)) == []

def test_zero_delivery_flag():
    assert (date(2026, 9, 3), "ZERO_DELIVERY") in delivery_anomalies(series(100, 100, 0, 100, 100))

def test_missing_day_flag():
    assert delivery_anomalies(series(100, 100, None, 100)) == [(date(2026, 9, 3), "MISSING_DAY")]

def test_spike_flag():
    flags = delivery_anomalies(series(100, 100, 100, 100, 1000))
    assert flags == [(date(2026, 9, 5), "SPIKE")]

def test_spike_needs_enough_history():
    assert delivery_anomalies(series(100, 1000)) == []     # too little data to call anything a spike

def test_spike_threshold_is_strict():
    assert delivery_anomalies(series(100, 100, 100, 100, 300)) == []   # exactly 3x is not flagged
