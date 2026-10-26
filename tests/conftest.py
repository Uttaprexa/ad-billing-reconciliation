import os
os.environ["DATABASE_URL"] = "sqlite://"   # in-memory DB for tests; must be set before importing app

import pytest
from fastapi.testclient import TestClient

from app.db import Base, engine
from app.main import app

PREP = {"X-User": "alice", "X-Role": "preparer"}
APPR = {"X-User": "bob", "X-Role": "approver"}


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return TestClient(app)


def invoice_body(**over):
    body = {"customer": "Acme Ltd", "issue_date": "2025-01-01", "due_date": "2025-01-31", "tax_rate": "10",
            "lines": [{"description": "Widget", "quantity": "3", "unit_price": "19.99"},
                      {"description": "Setup", "quantity": "1", "unit_price": "40.00"}]}
    body.update(over)
    return body


@pytest.fixture()
def approved(client):
    """An APPROVED invoice: subtotal 99.97, tax 10.00, total 109.97."""
    inv = client.post("/invoices", json=invoice_body(), headers=PREP).json()
    client.post(f"/invoices/{inv['id']}/submit", headers=PREP)
    r = client.post(f"/invoices/{inv['id']}/approve", headers=APPR)
    assert r.status_code == 200
    return r.json()
