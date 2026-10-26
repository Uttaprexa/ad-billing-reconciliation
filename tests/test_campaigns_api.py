from decimal import Decimal as D

from .conftest import APPR, PREP


def campaign_body(**over):
    body = {"code": "acme-q3", "customer": "Acme Ltd", "booked_impressions": 1_000_000, "cpm": "12.50",
            "flight_start": "2026-09-01", "flight_end": "2026-09-05", "tax_rate": "10"}
    body.update(over)
    return body


def delivery_csv(*per_day, code="ACME-Q3"):
    lines = [f"{code},2026-09-{i + 1:02d},{n}" for i, n in enumerate(per_day)]
    return {"file": ("d.csv", "campaign,date,impressions\n" + "\n".join(lines), "text/csv")}


def approved_campaign(client, **over):
    c = client.post("/campaigns", json=campaign_body(**over), headers=PREP).json()
    client.post(f"/invoices/{c['invoice_id']}/submit", headers=PREP)
    assert client.post(f"/invoices/{c['invoice_id']}/approve", headers=APPR).status_code == 200
    return c


def test_campaign_creates_cpm_invoice(client):
    r = client.post("/campaigns", json=campaign_body(), headers=PREP)
    assert r.status_code == 201
    c = r.json()
    assert c["code"] == "ACME-Q3" and c["delivery_pct"] == "0.00"
    inv = client.get(f"/invoices/{c['invoice_id']}", headers=PREP).json()
    assert (inv["subtotal"], inv["tax"], inv["total"]) == ("12500.00", "1250.00", "13750.00")

def test_campaign_validation_and_duplicates(client):
    for bad in (campaign_body(booked_impressions=0), campaign_body(cpm="0"), campaign_body(cpm="1.005"),
                campaign_body(flight_end="2026-08-01"), campaign_body(code="a b")):
        assert client.post("/campaigns", json=bad, headers=PREP).status_code == 422, bad
    client.post("/campaigns", json=campaign_body(), headers=PREP)
    assert client.post("/campaigns", json=campaign_body(code="ACME-Q3"), headers=PREP).status_code == 409
    assert client.post("/campaigns", json=campaign_body(code="x-1"), headers=APPR).status_code == 403

def test_delivery_dry_run_then_apply_and_idempotent(client):
    c = approved_campaign(client)
    f = delivery_csv(160_000, 160_000, 160_000, 160_000, 160_000)
    dry = client.post("/delivery", files=f, headers=PREP).json()
    assert dry["summary"]["ACCEPTED"] == 5 and dry["applied"] is False
    assert client.get(f"/campaigns/{c['id']}", headers=PREP).json()["delivered_impressions"] == 0
    assert client.post("/delivery?apply=true", files=f, headers=PREP).status_code == 403
    client.post("/delivery?apply=true", files=f, headers=APPR)
    again = client.post("/delivery?apply=true", files=f, headers=APPR).json()       # same file twice
    assert again["summary"]["DUPLICATE"] == 5 and again["summary"]["ACCEPTED"] == 0
    got = client.get(f"/campaigns/{c['id']}", headers=PREP).json()
    assert got["delivered_impressions"] == 800_000 and got["delivery_pct"] == "80.00"

def test_delivery_conflict_does_not_overwrite(client):
    c = approved_campaign(client)
    client.post("/delivery?apply=true", files=delivery_csv(100_000), headers=APPR)
    r = client.post("/delivery?apply=true", files=delivery_csv(999_999), headers=APPR).json()
    assert r["summary"]["CONFLICT"] == 1
    assert client.get(f"/campaigns/{c['id']}", headers=PREP).json()["delivered_impressions"] == 100_000

def test_delivery_anomalies_reported(client):
    approved_campaign(client)
    r = client.post("/delivery", files=delivery_csv(100, 100, 100, 100, 5000), headers=PREP).json()
    assert [(a["day"], a["flag"]) for a in r["anomalies"]] == [("2026-09-05", "SPIKE")]

def test_bad_delivery_csv_is_clear_400(client):
    r = client.post("/delivery", files={"file": ("x.csv", "a,b\n1,2", "text/csv")}, headers=PREP)
    assert r.status_code == 400 and "missing required columns" in r.json()["detail"]

def test_under_delivery_drafts_credit_that_needs_approval(client):
    c = approved_campaign(client)
    client.post("/delivery?apply=true", files=delivery_csv(*[160_000] * 5), headers=APPR)
    r = client.post(f"/campaigns/{c['id']}/settle?as_of=2026-09-10", headers=APPR)
    assert r.status_code == 200
    body = r.json()
    assert body["flags"] == ["UNDER_DELIVERY"]
    credit = body["credit"]
    assert credit["amount"] == "2750.00" and credit["status"] == "PENDING" and credit["created_by"] == "delivery-bot"
    assert client.get(f"/invoices/{c['invoice_id']}", headers=PREP).json()["balance"] == "13750.00"   # not yet effective
    assert client.post(f"/credits/{credit['id']}/approve", headers=APPR).status_code == 200
    assert client.get(f"/invoices/{c['invoice_id']}", headers=PREP).json()["balance"] == "11000.00"

def test_over_delivery_is_flagged_not_billed(client):
    c = approved_campaign(client)
    client.post("/delivery?apply=true", files=delivery_csv(*[300_000] * 5), headers=APPR)
    body = client.post(f"/campaigns/{c['id']}/settle?as_of=2026-09-10", headers=APPR).json()
    assert body["flags"] == ["OVER_DELIVERY"] and body["credit"] is None
    assert client.get(f"/invoices/{c['invoice_id']}", headers=PREP).json()["total"] == "13750.00"

def test_settle_guards(client):
    c = approved_campaign(client)
    assert client.post(f"/campaigns/{c['id']}/settle?as_of=2026-09-03", headers=APPR).status_code == 409   # flight running
    assert client.post(f"/campaigns/{c['id']}/settle?as_of=2026-09-10", headers=PREP).status_code == 403    # approver only
    assert client.post(f"/campaigns/{c['id']}/settle?as_of=2026-09-10", headers=APPR).status_code == 200
    assert client.post(f"/campaigns/{c['id']}/settle?as_of=2026-09-10", headers=APPR).status_code == 409    # only once

def test_cannot_settle_unapproved_invoice(client):
    c = client.post("/campaigns", json=campaign_body(), headers=PREP).json()
    assert client.post(f"/campaigns/{c['id']}/settle?as_of=2026-09-10", headers=APPR).status_code == 409

def test_settle_is_audited(client):
    c = approved_campaign(client)
    client.post("/delivery?apply=true", files=delivery_csv(*[160_000] * 5), headers=APPR)
    client.post(f"/campaigns/{c['id']}/settle?as_of=2026-09-10", headers=APPR)
    log = client.get("/audit?entity_type=campaign", headers=APPR).json()
    assert {r["action"] for r in log} >= {"CREATE", "APPLY_DELIVERY", "SETTLE"}
    credit_log = client.get("/audit?entity_type=credit", headers=APPR).json()
    assert credit_log[0]["actor"] == "delivery-bot"
