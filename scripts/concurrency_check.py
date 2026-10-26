"""Fire the SAME payments CSV at the running server from many threads at once and prove it is applied exactly once.
Run against PostgreSQL:   (terminal 1) python -m uvicorn app.main:app     (terminal 2) python scripts/concurrency_check.py
"""
import threading
import time

import httpx

BASE = "http://localhost:8000"
PREP = {"X-User": "alice", "X-Role": "preparer"}
APPR = {"X-User": "bob", "X-Role": "approver"}
N = 10

inv = httpx.post(f"{BASE}/invoices", headers=PREP, json={
    "customer": "Concurrency Co", "issue_date": "2026-01-01", "due_date": "2026-02-01", "tax_rate": "0",
    "lines": [{"description": "Service", "quantity": "1", "unit_price": "100.00"}]}).json()
httpx.post(f"{BASE}/invoices/{inv['id']}/submit", headers=PREP)
httpx.post(f"{BASE}/invoices/{inv['id']}/approve", headers=APPR)

pid = f"conc-{int(time.time())}"
csv_text = f"payment_id,reference,amount,date\n{pid},{inv['number']},40.00,2026-01-15\n"
codes = []

def upload():
    r = httpx.post(f"{BASE}/reconciliation?apply=true", headers=APPR, timeout=30,
                   files={"file": ("p.csv", csv_text, "text/csv")})
    codes.append(r.status_code)

threads = [threading.Thread(target=upload) for _ in range(N)]
[t.start() for t in threads]
[t.join() for t in threads]

final = httpx.get(f"{BASE}/invoices/{inv['id']}", headers=APPR).json()
print(f"HTTP codes: {sorted(codes)}")
print(f"paid={final['paid']}  balance={final['balance']}")
assert final["paid"] == "40.00", f"DOUBLE-APPLIED! paid={final['paid']}"
print(f"OK: {N} simultaneous uploads, payment applied exactly once")
