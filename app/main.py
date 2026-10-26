import html
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import Depends, FastAPI, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import audit
from .auth import Role, User, current_user, require
from .db import Base, engine, get_db
from .models import AuditLog, Campaign, Credit, DeliveryDay, Invoice, LineItem, Payment
from .money import ZERO, compute_totals
from .reconcile import CsvFormatError, InvoiceState, Status, aging, parse_payments_csv, reconcile
from .delivery import (DStatus, DeliveryCsvError, FlightInfo, classify_rows, credit_for_shortfall,
                       delivery_anomalies, parse_delivery_csv)
from .schemas import (AnomalyOut, AuditOut, CampaignIn, CampaignOut, CreditIn, CreditOut, DeliveryOut, InvoiceIn,
                      InvoiceOut, LineItemIn, ReconOut, RejectIn, SettleOut)

MAX_CSV_BYTES = 1_000_000


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)  # swap for Alembic migrations in a real deployment
    yield


app = FastAPI(title="Mini Invoicing & Reconciliation", lifespan=lifespan)
Preparer = Depends(require(Role.PREPARER))
Approver = Depends(require(Role.APPROVER))
AnyUser = Depends(current_user)


def utcnow():
    return datetime.now(timezone.utc)


def get_invoice(db: Session, invoice_id: int) -> Invoice:
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, f"invoice {invoice_id} not found")
    return inv


def conflict(msg: str):
    return HTTPException(409, msg)


def fill_invoice(inv: Invoice, data: InvoiceIn):
    totals, subtotal, tax, total = compute_totals([(l.quantity, l.unit_price) for l in data.lines], data.tax_rate)
    inv.customer, inv.issue_date, inv.due_date, inv.tax_rate = data.customer, data.issue_date, data.due_date, data.tax_rate
    inv.subtotal, inv.tax, inv.total = subtotal, tax, total
    inv.lines = [LineItem(description=l.description, quantity=l.quantity, unit_price=l.unit_price, line_total=t)
                 for l, t in zip(data.lines, totals)]


# ------------------------------------------------------------------ invoices
@app.post("/invoices", response_model=InvoiceOut, status_code=201)
def create_invoice(data: InvoiceIn, db: Session = Depends(get_db), user: User = Preparer):
    inv = Invoice(created_by=user.name, status="DRAFT")
    fill_invoice(inv, data)
    db.add(inv)
    db.flush()                                  # get the id, then derive the human-readable number
    inv.number = f"INV-{inv.id:06d}"
    audit.log(db, user, "CREATE", "invoice", inv.id, None, audit.snap_invoice(inv))
    db.commit()
    return inv


@app.get("/invoices", response_model=list[InvoiceOut])
def list_invoices(status: str | None = None, db: Session = Depends(get_db), user: User = AnyUser):
    q = select(Invoice).order_by(Invoice.id)
    if status:
        q = q.where(Invoice.status == status.upper())
    return db.scalars(q).all()


@app.get("/invoices/{invoice_id}", response_model=InvoiceOut)
def read_invoice(invoice_id: int, db: Session = Depends(get_db), user: User = AnyUser):
    return get_invoice(db, invoice_id)


@app.put("/invoices/{invoice_id}", response_model=InvoiceOut)
def update_invoice(invoice_id: int, data: InvoiceIn, db: Session = Depends(get_db), user: User = Preparer):
    inv = get_invoice(db, invoice_id)
    if inv.status not in ("DRAFT", "REJECTED"):
        raise conflict(f"only DRAFT or REJECTED invoices can be edited (this one is {inv.status})")
    before = audit.snap_invoice(inv)
    fill_invoice(inv, data)
    inv.status, inv.rejection_reason = "DRAFT", None
    db.flush()
    audit.log(db, user, "UPDATE", "invoice", inv.id, before, audit.snap_invoice(inv))
    db.commit()
    return inv


@app.post("/invoices/{invoice_id}/submit", response_model=InvoiceOut)
def submit_invoice(invoice_id: int, db: Session = Depends(get_db), user: User = Preparer):
    inv = get_invoice(db, invoice_id)
    if inv.status != "DRAFT":
        raise conflict(f"only DRAFT invoices can be submitted (this one is {inv.status})")
    inv.status = "PENDING"
    audit.log(db, user, "SUBMIT", "invoice", inv.id, {"status": "DRAFT"}, {"status": "PENDING"})
    db.commit()
    return inv


def decide_invoice(db, inv, user, new_status, reason=None):
    if inv.status != "PENDING":
        raise conflict(f"only PENDING invoices can be {new_status.lower()} (this one is {inv.status})")
    if inv.created_by == user.name:  # maker-checker: you cannot approve your own work
        raise HTTPException(403, "the creator of an invoice cannot approve or reject it")
    inv.status, inv.decided_by, inv.decided_at, inv.rejection_reason = new_status, user.name, utcnow(), reason
    audit.log(db, user, "APPROVE" if new_status == "APPROVED" else "REJECT",
              "invoice", inv.id, {"status": "PENDING"}, {"status": new_status, "reason": reason})
    db.commit()
    return inv


@app.post("/invoices/{invoice_id}/approve", response_model=InvoiceOut)
def approve_invoice(invoice_id: int, db: Session = Depends(get_db), user: User = Approver):
    return decide_invoice(db, get_invoice(db, invoice_id), user, "APPROVED")


@app.post("/invoices/{invoice_id}/reject", response_model=InvoiceOut)
def reject_invoice(invoice_id: int, body: RejectIn, db: Session = Depends(get_db), user: User = Approver):
    return decide_invoice(db, get_invoice(db, invoice_id), user, "REJECTED", body.reason)


@app.get("/invoices/{invoice_id}/preview", response_class=HTMLResponse)
def preview_invoice(invoice_id: int, db: Session = Depends(get_db), user: User = AnyUser):
    i, e = get_invoice(db, invoice_id), html.escape  # escape user text: customer names are untrusted input
    rows = "".join(f"<tr><td>{e(l.description)}</td><td class=r>{l.quantity.normalize():f}</td>"
                   f"<td class=r>{l.unit_price}</td><td class=r>{l.line_total}</td></tr>" for l in i.lines)
    extra = ""
    if i.credited:
        extra += f"<tr><td colspan=3 class=r>Credits</td><td class=r>-{i.credited}</td></tr>"
    if i.paid:
        extra += f"<tr><td colspan=3 class=r>Payments</td><td class=r>-{i.paid}</td></tr>"
    return HTMLResponse(f"""<!doctype html><meta charset=utf-8><title>{e(i.number)}</title>
<style>body{{font:14px sans-serif;max-width:640px;margin:2rem auto}}table{{width:100%;border-collapse:collapse}}
td,th{{padding:6px;border-bottom:1px solid #ddd;text-align:left}}.r{{text-align:right}}
.tag{{float:right;padding:2px 8px;border:1px solid #999;border-radius:4px}}</style>
<h1>Invoice {e(i.number)} <span class=tag>{i.status}</span></h1>
<p>Bill to: <b>{e(i.customer)}</b><br>Issued {i.issue_date} · Due {i.due_date}</p>
<table><tr><th>Description<th class=r>Qty<th class=r>Unit price<th class=r>Amount</tr>{rows}
<tr><td colspan=3 class=r>Subtotal</td><td class=r>{i.subtotal}</td></tr>
<tr><td colspan=3 class=r>Tax ({i.tax_rate.normalize():f}%)</td><td class=r>{i.tax}</td></tr>
<tr><td colspan=3 class=r><b>Total</b></td><td class=r><b>{i.total}</b></td></tr>{extra}
<tr><td colspan=3 class=r><b>Balance due</b></td><td class=r><b>{i.balance}</b></td></tr></table>""")


# ------------------------------------------------------------------ credits
@app.post("/invoices/{invoice_id}/credits", response_model=CreditOut, status_code=201)
def create_credit(invoice_id: int, data: CreditIn, db: Session = Depends(get_db), user: User = Preparer):
    inv = get_invoice(db, invoice_id)
    if inv.status != "APPROVED":
        raise conflict("credits can only be raised against APPROVED invoices")
    if data.amount > inv.balance:
        raise conflict(f"credit {data.amount} exceeds the invoice balance {inv.balance}")
    c = Credit(invoice_id=inv.id, amount=data.amount, reason=data.reason, created_by=user.name)
    db.add(c)
    db.flush()
    audit.log(db, user, "CREATE", "credit", c.id, None, audit.snap_credit(c))
    db.commit()
    return c


def decide_credit(db, credit_id, user, new_status):
    c = db.get(Credit, credit_id)
    if not c:
        raise HTTPException(404, f"credit {credit_id} not found")
    if c.status != "PENDING":
        raise conflict(f"credit is already {c.status}")
    if c.created_by == user.name:
        raise HTTPException(403, "the creator of a credit cannot approve or reject it")
    if new_status == "APPROVED":
        inv = db.get(Invoice, c.invoice_id)   # re-check: the balance may have changed since the credit was raised
        if c.amount > inv.balance:
            raise conflict(f"credit {c.amount} now exceeds the invoice balance {inv.balance}")
    c.status, c.decided_by, c.decided_at = new_status, user.name, utcnow()
    audit.log(db, user, "APPROVE" if new_status == "APPROVED" else "REJECT", "credit", c.id,
              {"status": "PENDING"}, {"status": new_status})
    db.commit()
    return c


@app.post("/credits/{credit_id}/approve", response_model=CreditOut)
def approve_credit(credit_id: int, db: Session = Depends(get_db), user: User = Approver):
    return decide_credit(db, credit_id, user, "APPROVED")


@app.post("/credits/{credit_id}/reject", response_model=CreditOut)
def reject_credit(credit_id: int, db: Session = Depends(get_db), user: User = Approver):
    return decide_credit(db, credit_id, user, "REJECTED")


# ------------------------------------------------------------------ reconciliation
@app.post("/reconciliation", response_model=ReconOut)
async def run_reconciliation(file: UploadFile, apply: bool = False, db: Session = Depends(get_db),
                             user: User = AnyUser):
    """Upload a payments CSV (payment_id,reference,amount,date). Default is a dry run;
    apply=true (approvers only) records the matched / partial / overpaid payments."""
    if apply and user.role != Role.APPROVER:
        raise HTTPException(403, "only approvers may apply payments (dry runs are open to everyone)")
    raw = await file.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        raise HTTPException(413, "CSV larger than 1 MB")
    try:
        rows = parse_payments_csv(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise HTTPException(400, "file is not valid UTF-8 text")
    except CsvFormatError as ex:
        raise HTTPException(400, str(ex))

    q = select(Invoice)
    if apply:
        q = q.with_for_update()   # PostgreSQL row locks: concurrent applies queue up instead of double-counting (no-op on SQLite)
    invoices = db.scalars(q).all()
    by_number = {i.number: i for i in invoices}
    states = {i.number: InvoiceState(i.number, i.status, i.issue_date, i.balance) for i in invoices}
    payments = db.scalars(select(Payment)).all()
    num_of = {i.id: i.number for i in invoices}
    results = reconcile(states, rows,
                        known_payment_ids=frozenset(p.payment_id for p in payments),
                        known_keys=frozenset((num_of[p.invoice_id], p.amount, p.paid_on) for p in payments))

    if apply:
        try:
            for r in results:
                if r.status in (Status.MATCHED, Status.PARTIAL, Status.OVERPAID):
                    inv = by_number[r.invoice_number]
                    p = Payment(payment_id=r.payment_id, invoice_id=inv.id, amount=r.amount, paid_on=r.paid_on,
                                reference=r.reference[:300], applied_by=user.name)
                    db.add(p)
                    db.flush()
                    audit.log(db, user, "APPLY_PAYMENT", "payment", p.id, None,
                              {"payment_id": p.payment_id, "invoice": inv.number, "amount": str(p.amount),
                               "result": r.status.value, "flags": r.flags})
            db.commit()
        except IntegrityError:   # last line of defence: UNIQUE(payment_id) fired because of a race
            db.rollback()
            raise HTTPException(409, "a concurrent upload recorded some of these payments; re-run to see current state")

    summary: dict[str, int] = {s.value: 0 for s in Status}
    for r in results:
        summary[r.status.value] += 1
    return ReconOut(applied=apply, summary=summary, rows=results)


# ------------------------------------------------------------------ reports & audit
@app.get("/reports/aging")
def aging_report(as_of: date | None = None, db: Session = Depends(get_db), user: User = AnyUser):
    approved = db.scalars(select(Invoice).where(Invoice.status == "APPROVED")).all()
    buckets = aging([(i.due_date, i.balance) for i in approved], as_of or date.today())
    return {"as_of": (as_of or date.today()).isoformat(), "buckets": {k: str(v) for k, v in buckets.items()},
            "total_outstanding": str(sum(buckets.values(), ZERO))}


@app.get("/audit", response_model=list[AuditOut])
def audit_log(entity_type: str | None = None, entity_id: int | None = None, actor: str | None = None,
              limit: int = 100, db: Session = Depends(get_db), user: User = Approver):
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(max(limit, 1), 500))
    if entity_type:
        q = q.where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        q = q.where(AuditLog.entity_id == entity_id)
    if actor:
        q = q.where(AuditLog.actor == actor)
    return db.scalars(q).all()


# ------------------------------------------------------------------ campaigns (CPM billing) & delivery
def campaign_out(c: Campaign) -> CampaignOut:
    pct = (Decimal(c.delivered) * 100 / Decimal(c.booked_impressions)).quantize(Decimal("0.01"))
    inv = c.__dict__.get("_inv")
    return CampaignOut(id=c.id, code=c.code, customer=c.customer, booked_impressions=c.booked_impressions,
                       delivered_impressions=c.delivered, delivery_pct=pct, cpm=c.cpm, flight_start=c.flight_start,
                       flight_end=c.flight_end, invoice_id=c.invoice_id, invoice_number=inv.number if inv else "",
                       settled=c.settled_at is not None)


def get_campaign(db: Session, campaign_id: int, lock: bool = False) -> Campaign:
    q = select(Campaign).where(Campaign.id == campaign_id)
    c = db.scalars(q.with_for_update() if lock else q).first()
    if not c:
        raise HTTPException(404, f"campaign {campaign_id} not found")
    c._inv = db.get(Invoice, c.invoice_id)
    return c


@app.post("/campaigns", response_model=CampaignOut, status_code=201)
def create_campaign(data: CampaignIn, db: Session = Depends(get_db), user: User = Preparer):
    """Books an insertion order and drafts its invoice: impressions/1000 x CPM, as one line item."""
    code = data.code.upper()
    if db.scalars(select(Campaign).where(Campaign.code == code)).first():
        raise conflict(f"campaign {code} already exists")
    inv_in = InvoiceIn(
        customer=data.customer, issue_date=data.flight_start, due_date=data.flight_end + timedelta(days=30),
        tax_rate=data.tax_rate,
        lines=[LineItemIn(description=f"{code}: {data.booked_impressions:,} impressions @ ${data.cpm} CPM",
                          quantity=Decimal(data.booked_impressions) / Decimal(1000), unit_price=data.cpm)])
    inv = Invoice(created_by=user.name, status="DRAFT")
    fill_invoice(inv, inv_in)
    db.add(inv)
    db.flush()
    inv.number = f"INV-{inv.id:06d}"
    c = Campaign(code=code, customer=data.customer, booked_impressions=data.booked_impressions, cpm=data.cpm,
                 flight_start=data.flight_start, flight_end=data.flight_end, invoice_id=inv.id, created_by=user.name)
    db.add(c)
    db.flush()
    audit.log(db, user, "CREATE", "invoice", inv.id, None, audit.snap_invoice(inv))
    audit.log(db, user, "CREATE", "campaign", c.id, None,
              {"code": code, "booked_impressions": c.booked_impressions, "cpm": str(c.cpm), "invoice": inv.number})
    db.commit()
    c._inv = inv
    return campaign_out(c)


@app.get("/campaigns/{campaign_id}", response_model=CampaignOut)
def read_campaign(campaign_id: int, db: Session = Depends(get_db), user: User = AnyUser):
    return campaign_out(get_campaign(db, campaign_id))


@app.post("/delivery", response_model=DeliveryOut)
async def upload_delivery(file: UploadFile, apply: bool = False, db: Session = Depends(get_db),
                          user: User = AnyUser):
    """Daily delivery CSV (campaign,date,impressions). Dry run by default; apply=true (approvers) stores new days.
    Never overwrites a stored day: a different number for the same day is reported as CONFLICT."""
    if apply and user.role != Role.APPROVER:
        raise HTTPException(403, "only approvers may apply delivery data (dry runs are open to everyone)")
    raw = await file.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        raise HTTPException(413, "CSV larger than 1 MB")
    try:
        rows = parse_delivery_csv(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise HTTPException(400, "file is not valid UTF-8 text")
    except DeliveryCsvError as ex:
        raise HTTPException(400, str(ex))

    campaigns = {c.code: c for c in db.scalars(select(Campaign)).all()}
    flights = {k: FlightInfo(k, c.flight_start, c.flight_end) for k, c in campaigns.items()}
    existing = {(c.code, d.day): d.impressions for c in campaigns.values() for d in c.deliveries}
    results = classify_rows(flights, existing, rows)

    accepted = [r for r in results if r.status == DStatus.ACCEPTED]
    if apply:
        try:
            for r in accepted:
                db.add(DeliveryDay(campaign_id=campaigns[r.campaign].id, day=r.day, impressions=r.impressions))
            if accepted:
                audit.log(db, user, "APPLY_DELIVERY", "campaign", campaigns[accepted[0].campaign].id, None,
                          {"days_added": len(accepted), "campaigns": sorted({r.campaign for r in accepted})})
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "a concurrent upload recorded some of these days; re-run to see current state")

    # anomaly rules run on stored days + the days in this file
    series: dict[str, dict[date, int]] = {}
    for (code, day), n in existing.items():
        series.setdefault(code, {})[day] = n
    for r in accepted:
        series.setdefault(r.campaign, {})[r.day] = r.impressions
    touched = {r.campaign for r in results if r.status in (DStatus.ACCEPTED, DStatus.DUPLICATE)}
    anomalies = [AnomalyOut(campaign=code, day=d, flag=f, impressions=series[code].get(d, 0))
                 for code in sorted(touched) for d, f in delivery_anomalies(series[code])]
    summary = {s.value: 0 for s in DStatus}
    for r in results:
        summary[r.status.value] += 1
    return DeliveryOut(applied=apply, summary=summary, rows=results, anomalies=anomalies)


@app.post("/campaigns/{campaign_id}/settle", response_model=SettleOut)
def settle_campaign(campaign_id: int, as_of: date | None = None, db: Session = Depends(get_db),
                    user: User = Approver):
    """After the flight ends: compare delivered vs booked. Under-delivery auto-drafts a PENDING credit (makegood)
    that still needs an approver. Over-delivery is NOT billed; it is only flagged."""
    c = get_campaign(db, campaign_id, lock=True)
    if c.settled_at:
        raise conflict("campaign is already settled")
    if (as_of or date.today()) <= c.flight_end:
        raise conflict(f"flight has not ended yet (ends {c.flight_end})")
    inv = c._inv
    if inv.status != "APPROVED":
        raise conflict(f"invoice {inv.number} is {inv.status}; it must be APPROVED before settling")
    flags, credit = [], None
    delivered = c.delivered
    if delivered > c.booked_impressions:
        flags.append("OVER_DELIVERY")
    elif delivered < c.booked_impressions:
        flags.append("UNDER_DELIVERY")
        amount = min(credit_for_shortfall(c.booked_impressions, delivered, c.cpm, inv.tax_rate), inv.balance)
        if amount > 0:
            credit = Credit(invoice_id=inv.id, amount=amount, created_by="delivery-bot",
                            reason=f"Under-delivery makegood: {delivered:,} of {c.booked_impressions:,} impressions "
                                   f"delivered on {c.code}")
            db.add(credit)
            db.flush()
            audit.log(db, User(name="delivery-bot", role=Role.PREPARER), "CREATE", "credit", credit.id, None,
                      audit.snap_credit(credit))
    c.settled_at = utcnow()
    audit.log(db, user, "SETTLE", "campaign", c.id, None,
              {"delivered": delivered, "booked": c.booked_impressions, "flags": flags,
               "credit": str(credit.amount) if credit else None})
    db.commit()
    out = campaign_out(c)
    return SettleOut(campaign=c.code, booked_impressions=c.booked_impressions, delivered_impressions=delivered,
                     delivery_pct=out.delivery_pct, flags=flags, credit=CreditOut.model_validate(credit) if credit else None)
