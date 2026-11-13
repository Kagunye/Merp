"""Asking for money and booking it once it is confirmed.

    request_payment()  sends an M-Pesa prompt, or makes a Pesapal / DPO
                       checkout link, for an invoice, a till ticket or any amount
    refresh()          asks the provider where a request stands and, on PAID,
                       books the receipt (callbacks, the status button and the
                       till's polling all come through here)
    paybill_received() money paid straight to the paybill / till: matched to
                       the invoice whose number the customer typed

A request is moved off PENDING by a conditional UPDATE inside the
transaction, so two callbacks arriving together (or a callback and the till
asking) book the receipt exactly once.
"""
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .backends import GatewayError, driver, normalise_phone
from .models import PaybillReceipt, PaymentGateway, PaymentRequest

Z = Decimal("0")
METHOD = {"MPESA": "MPESA", "PESAPAL": "CARD", "DPO": "CARD"}


def gateways_for(company, active_only=True):
    qs = PaymentGateway.objects.filter(company=company)
    return qs.filter(is_active=True) if active_only else qs


def gateway(company, provider, active_only=True):
    return gateways_for(company, active_only).filter(provider=provider).first()


def _base(gw, request=None):
    if gw.callback_base:
        return gw.callback_base.rstrip("/")
    if request is not None:
        return request.build_absolute_uri("/").rstrip("/")
    return ""


def request_payment(company, provider, amount, *, user=None, request=None, invoice=None, pos_session=None,
                    reference="", description="", phone="", email="", payer=""):
    gw = gateway(company, provider)
    if gw is None:
        raise ValidationError(f"{dict(PaymentGateway.PROVIDERS).get(provider, provider)} is not switched on for this company.")
    try:
        amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise ValidationError("Enter the amount to ask for.")
    if amount <= 0:
        raise ValidationError("The amount must be more than zero.")
    if provider == "MPESA":
        try:
            phone = normalise_phone(phone)
        except GatewayError as exc:
            raise ValidationError(str(exc))
        if amount != amount.to_integral_value():
            raise ValidationError("M-Pesa takes whole shillings: round the amount.")
    if invoice is not None:
        if invoice.company_id != company.pk:
            raise ValidationError("That invoice belongs to another company.")
        if invoice.status not in ("SENT", "PARTIAL", "OVERDUE") or invoice.outstanding_amount <= 0:
            raise ValidationError(f"{invoice.reference} is not an open, posted invoice: nothing to pay.")
        if amount > invoice.outstanding_amount:
            raise ValidationError(f"{invoice.reference} is only owed {invoice.outstanding_amount}.")
        reference = reference or invoice.reference
        description = description or f"Invoice {invoice.reference}"
    req = PaymentRequest.objects.create(
        company=company, gateway=gw, purpose="INVOICE" if invoice else ("POS" if pos_session else "OTHER"),
        invoice=invoice, pos_session=pos_session, reference=(reference or "PAYMENT")[:40],
        description=(description or reference or "Payment")[:100], amount=amount,
        currency=getattr(invoice, "currency", None) or company.currency, phone=phone, email=email,
        payer=payer[:120], created_by=user)
    base = _base(gw, request)
    callback = base + reverse("payments:callback", args=[gw.provider.lower(), req.callback_key])
    back = base + reverse("payments:returned", args=[req.callback_key])
    extra = {"ipn_url": base + reverse("payments:pesapal_ipn", args=[gw.webhook_key])} if provider == "PESAPAL" else {}
    try:
        out = driver(gw).start(req, callback, back, **extra)
    except GatewayError as exc:
        req.status, req.note = "FAILED", str(exc)[:255]
        req.save(update_fields=["status", "note"])
        raise ValidationError(str(exc))
    req.provider_ref = out.get("provider_ref", "")
    req.checkout_url = out.get("checkout_url", "")
    if out.get("checkout_url", "").startswith("/"):
        req.checkout_url = base + out["checkout_url"]
    req.raw = {"start": out.get("raw", {})}
    req.save(update_fields=["provider_ref", "checkout_url", "raw"])
    return req


def refresh(req, *, hint=None):
    """Ask the provider; book the receipt the first time it says PAID."""
    if req.status != "PENDING":
        return req
    gw = req.gateway
    try:
        result = driver(gw).status(req)
    except GatewayError as exc:
        req.note = str(exc)[:255]
        req.save(update_fields=["note"])
        return req
    status = result.get("status", "PENDING")
    if status == "PENDING":
        return req
    receipt = (result.get("receipt") or (hint or {}).get("receipt") or "")[:60]
    paid = result.get("amount")
    with transaction.atomic():
        # Claim it: only one caller moves a request off PENDING.
        moved = PaymentRequest.objects.filter(pk=req.pk, status="PENDING").update(
            status=status, receipt=receipt, completed_at=timezone.now(), note=(result.get("note") or "")[:255])
        if not moved:
            req.refresh_from_db()
            return req
        req.refresh_from_db()
        raw = dict(req.raw or {})
        raw["status"] = result.get("raw", {})
        if hint:
            raw["callback"] = hint
        req.raw = raw
        if status == "PAID" and paid not in (None, ""):
            try:
                if Decimal(str(paid)) + Decimal("0.5") < req.amount:
                    req.note = f"Paid {paid}, less than the {req.amount} asked."
            except (InvalidOperation, ValueError):
                pass
        req.save(update_fields=["raw", "note"])
        if status == "PAID" and req.invoice_id:
            payment = book_receipt(req.company, req.invoice, req.amount, req.gateway, receipt or req.provider_ref,
                                   user=req.created_by, note=f"{req.gateway.get_provider_display()} {req.reference}")
            req.customer_payment = payment
            req.save(update_fields=["customer_payment"])
    return req


def book_receipt(company, invoice, amount, gw, code, *, user=None, note=""):
    """A customer receipt for money a gateway confirmed, applied to the invoice."""
    from apps.accounting.gl_service import post_customer_payment
    from apps.sales.models import CustomerPayment
    from apps.sales.views import _claim_reference
    amount = Decimal(str(amount))
    bank = gw.settlement_account
    if bank is None and gw.provider == "MPESA":
        from apps.banking.models import BankAccount
        bank = BankAccount.objects.filter(company=company, receives_pos_mpesa=True).first()
    payment = CustomerPayment(
        company=company, customer=invoice.customer, payment_date=timezone.localdate(), amount=amount,
        payment_method=METHOD.get(gw.provider, "OTHER"), bank_account=bank, transaction_reference=code,
        currency=invoice.currency, exchange_rate=getattr(invoice, "exchange_rate", None) or Decimal("1"),
        notes=note)
    payment.reference = _claim_reference(CustomerPayment, company, "PAY-", "")
    payment.save()
    apply = min(amount, invoice.outstanding_amount)
    post_customer_payment(payment, user=user, invoice_allocations=[(invoice, apply)] if apply > 0 else None)
    return payment


def paybill_received(gw, body):
    """A C2B confirmation: keep it once, and book it if the account number is an open invoice."""
    from apps.sales.models import Invoice
    trans = str(body.get("TransID") or "").strip()
    if not trans:
        raise ValidationError("No TransID.")
    try:
        amount = Decimal(str(body.get("TransAmount") or "0"))
    except InvalidOperation:
        amount = Z
    account = str(body.get("BillRefNumber") or "").strip()
    payer = " ".join(str(body.get(k) or "") for k in ("FirstName", "MiddleName", "LastName")).strip()
    with transaction.atomic():
        receipt, created = PaybillReceipt.objects.get_or_create(
            gateway=gw, trans_id=trans,
            defaults={"company": gw.company, "amount": amount, "account": account[:60],
                      "msisdn": str(body.get("MSISDN") or "")[:80], "payer": payer[:120], "raw": body,
                      "paid_at": timezone.now()})
        if not created:
            return receipt
        invoice = None
        if account:
            invoice = Invoice.objects.filter(company=gw.company, reference__iexact=account,
                                             outstanding_amount__gt=0).first()
        if invoice is None:
            receipt.note = "No open invoice with that account number: match it by hand."
            receipt.save(update_fields=["note"])
            return receipt
        if invoice.currency != gw.company.currency:
            receipt.note = f"{invoice.reference} is billed in {invoice.currency}: match it by hand."
            receipt.save(update_fields=["note"])
            return receipt
        payment = book_receipt(gw.company, invoice, amount, gw, trans, note=f"Paybill {account}")
        receipt.invoice, receipt.customer_payment = invoice, payment
        receipt.save(update_fields=["invoice", "customer_payment"])
    return receipt


def simulate(req, result, receipt=""):
    """The simulator page (or a test) decides how a simulated payment ends."""
    if req.gateway.environment != "SIMULATOR":
        raise ValidationError("Only simulated payments can be decided here.")
    raw = dict(req.raw or {})
    raw["simulated_result"] = result
    if receipt:
        raw["simulated_receipt"] = receipt
    req.raw = raw
    req.save(update_fields=["raw"])
    return refresh(req)
