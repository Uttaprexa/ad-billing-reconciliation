from decimal import Decimal as D

from .conftest import APPR, PREP, invoice_body


def csv_file(*lines):
    return {"file": ("pay.csv", "payment_id,reference,amount,date\n" + "\n".join(lines), "text/csv")}


# ---------- totals & validation ----------
def test_create_invoice_totals_are_exact(client):
    r = client.post("/invoices", json=invoice_body(), headers=PREP)
    assert r.status_code == 201
    b = r.json()
    assert b["number"] == "INV-000001" and b["status"] == "DRAFT"
    assert (b["subtotal"], b["tax"], b["total"]) == ("99.97", "10.00", "109.97")   # 99.97*10% = 9.997 -> 10.00
    assert isinstance(b["total"], str)                                              # money never leaves as a float

def test_validation_errors(client):
    bad = [invoice_body(lines=[]),                                                                    # no lines
           invoice_body(lines=[{"description": "x", "quantity": "-1", "unit_price": "5"}]),           # negative qty
           invoice_body(lines=[{"description": "x", "quantity": "1", "unit_price": "-5"}]),           # negative price
           invoice_body(lines=[{"description": "x", "quantity": "1", "unit_price": "10.005"}]),       # sub-cent price
           invoice_body(due_date="2024-12-01"),                                                       # due before issue
           invoice_body(tax_rate="150"), invoice_body(customer="")]
    for body in bad:
        assert client.post("/invoices", json=body, headers=PREP).status_code == 422, body

def test_preview_escapes_html(client):
    inv = client.post("/invoices", json=invoice_body(customer="<script>x</script>"), headers=PREP).json()
    html = client.get(f"/invoices/{inv['id']}/preview", headers=PREP).text
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html and "109.97" in html


# ---------- permissions & workflow ----------
def test_preparer_cannot_approve_and_approver_cannot_create(client):
    inv = client.post("/invoices", json=invoice_body(), headers=PREP).json()
    client.post(f"/invoices/{inv['id']}/submit", headers=PREP)
    assert client.post(f"/invoices/{inv['id']}/approve", headers=PREP).status_code == 403
    assert client.post("/invoices", json=invoice_body(), headers=APPR).status_code == 403

def test_missing_or_bad_identity(client):
    assert client.get("/invoices").status_code == 422
    assert client.get("/invoices", headers={"X-User": "x", "X-Role": "admin"}).status_code == 401

def test_maker_checker_same_person_cannot_approve(client):
    me = {"X-User": "alice", "X-Role": "approver"}
    inv = client.post("/invoices", json=invoice_body(), headers=PREP).json()
    client.post(f"/invoices/{inv['id']}/submit", headers=PREP)
    assert client.post(f"/invoices/{inv['id']}/approve", headers=me).status_code == 403

def test_state_machine(client):
    inv = client.post("/invoices", json=invoice_body(), headers=PREP).json()
    assert client.post(f"/invoices/{inv['id']}/approve", headers=APPR).status_code == 409   # not submitted yet
    client.post(f"/invoices/{inv['id']}/submit", headers=PREP)
    assert client.post(f"/invoices/{inv['id']}/submit", headers=PREP).status_code == 409    # double submit
    assert client.put(f"/invoices/{inv['id']}", json=invoice_body(), headers=PREP).status_code == 409  # locked

def test_reject_needs_reason_then_edit_and_resubmit(client):
    inv = client.post("/invoices", json=invoice_body(), headers=PREP).json()
    client.post(f"/invoices/{inv['id']}/submit", headers=PREP)
    assert client.post(f"/invoices/{inv['id']}/reject", json={"reason": ""}, headers=APPR).status_code == 422
    assert client.post(f"/invoices/{inv['id']}/reject", json={"reason": "wrong tax"}, headers=APPR).json()["status"] == "REJECTED"
    assert client.put(f"/invoices/{inv['id']}", json=invoice_body(tax_rate="5"), headers=PREP).json()["status"] == "DRAFT"


# ---------- credits ----------
def test_credit_flow_reduces_balance_only_when_approved(client, approved):
    iid = approved["id"]
    c = client.post(f"/invoices/{iid}/credits", json={"amount": "9.97", "reason": "damaged item"}, headers=PREP).json()
    assert client.get(f"/invoices/{iid}", headers=PREP).json()["balance"] == "109.97"       # pending: no effect
    client.post(f"/credits/{c['id']}/approve", headers=APPR)
    assert client.get(f"/invoices/{iid}", headers=PREP).json()["balance"] == "100.00"

def test_credit_validation(client, approved):
    iid = approved["id"]
    for amount in ("-5.00", "0", "200.00"):
        r = client.post(f"/invoices/{iid}/credits", json={"amount": amount, "reason": "bad"}, headers=PREP)
        assert r.status_code in (409, 422), amount

def test_cannot_credit_unapproved_invoice(client):
    inv = client.post("/invoices", json=invoice_body(), headers=PREP).json()
    r = client.post(f"/invoices/{inv['id']}/credits", json={"amount": "1.00", "reason": "nope"}, headers=PREP)
    assert r.status_code == 409


# ---------- reconciliation end-to-end ----------
def test_dry_run_changes_nothing_and_apply_requires_approver(client, approved):
    f = csv_file("p1,INV-000001,109.97,2025-02-01")
    r = client.post("/reconciliation", files=f, headers=PREP).json()
    assert r["summary"]["MATCHED"] == 1 and r["applied"] is False
    assert client.get(f"/invoices/{approved['id']}", headers=PREP).json()["paid"] == "0.00"
    assert client.post("/reconciliation?apply=true", files=f, headers=PREP).status_code == 403

def test_apply_partial_then_rerun_flags_duplicates(client, approved):
    f = csv_file("p1,INV-000001,50.00,2025-02-01")
    r = client.post("/reconciliation?apply=true", files=f, headers=APPR).json()
    assert r["rows"][0]["status"] == "PARTIAL" and r["rows"][0]["balance_after"] == "59.97"
    assert client.get(f"/invoices/{approved['id']}", headers=PREP).json()["balance"] == "59.97"
    again = client.post("/reconciliation?apply=true", files=f, headers=APPR).json()          # same file uploaded twice
    assert again["rows"][0]["status"] == "DUPLICATE"
    assert client.get(f"/invoices/{approved['id']}", headers=PREP).json()["balance"] == "59.97"   # unchanged

def test_credit_then_payment_reconciles_to_zero(client, approved):
    iid = approved["id"]
    c = client.post(f"/invoices/{iid}/credits", json={"amount": "9.97", "reason": "damaged"}, headers=PREP).json()
    client.post(f"/credits/{c['id']}/approve", headers=APPR)
    r = client.post("/reconciliation?apply=true", files=csv_file("p1,INV-000001,100.00,2025-02-01"), headers=APPR).json()
    assert r["rows"][0]["status"] == "MATCHED"

def test_bad_csv_is_a_clear_400(client):
    r = client.post("/reconciliation", files={"file": ("x.csv", "foo,bar\n1,2", "text/csv")}, headers=PREP)
    assert r.status_code == 400 and "missing required columns" in r.json()["detail"]


# ---------- audit trail ----------
def test_audit_trail_records_who_what_when(client, approved):
    client.post("/reconciliation?apply=true", files=csv_file("p1,INV-000001,10.00,2025-02-01"), headers=APPR)
    rows = client.get("/audit", headers=APPR).json()
    actions = [(r["actor"], r["action"], r["entity_type"]) for r in reversed(rows)]
    assert actions == [("alice", "CREATE", "invoice"), ("alice", "SUBMIT", "invoice"),
                       ("bob", "APPROVE", "invoice"), ("bob", "APPLY_PAYMENT", "payment")]
    assert all(r["ts"] for r in rows)
    assert rows[-1]["after"]["total"] == "109.97"        # snapshot of the created invoice, money as string

def test_audit_is_approver_only_and_has_no_write_endpoints(client):
    assert client.get("/audit", headers=PREP).status_code == 403
    assert client.delete("/audit/1", headers=APPR).status_code == 404   # no such route: log is append-only


# ---------- aging ----------
def test_aging_report(client, approved):
    r = client.get("/reports/aging?as_of=2025-03-15", headers=PREP).json()      # due 2025-01-31 -> 43 days late
    assert r["buckets"]["31-60"] == "109.97" and r["total_outstanding"] == "109.97"
