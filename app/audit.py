import json
from decimal import Decimal

from sqlalchemy.orm import Session

from .auth import User
from .models import AuditLog, Credit, Invoice


def _clean(d):  # make Decimal/date JSON-safe (money stored as strings, never floats)
    return json.loads(json.dumps(d, default=str))


def snap_invoice(i: Invoice) -> dict:
    return _clean({"number": i.number, "customer": i.customer, "status": i.status, "issue_date": i.issue_date,
                   "due_date": i.due_date, "tax_rate": i.tax_rate, "subtotal": i.subtotal, "tax": i.tax, "total": i.total,
                   "lines": [{"description": l.description, "quantity": l.quantity, "unit_price": l.unit_price,
                              "line_total": l.line_total} for l in i.lines]})


def snap_credit(c: Credit) -> dict:
    return _clean({"invoice_id": c.invoice_id, "amount": c.amount, "reason": c.reason, "status": c.status})


def log(db: Session, user: User, action: str, entity_type: str, entity_id: int, before=None, after=None):
    """Written in the SAME transaction as the change: both commit or neither does."""
    db.add(AuditLog(actor=user.name, role=user.role.value, action=action, entity_type=entity_type,
                    entity_id=entity_id, before=before, after=after))
