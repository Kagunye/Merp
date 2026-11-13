import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.core.tenancy import find_anywhere

from . import services
from .backends import GatewayError, driver, stk_callback_items
from .forms import GatewayForm
from .models import PaybillReceipt, PaymentGateway, PaymentRequest

PROVIDERS = [p for p, _ in PaymentGateway.PROVIDERS]
BLURB = {
    "MPESA": "Send an M-Pesa prompt to the customer's phone, and book paybill / till payments automatically.",
    "PESAPAL": "A checkout link: the customer pays by card, M-Pesa or Airtel Money on Pesapal's page.",
    "DPO": "A checkout link on DPO Pay: cards and mobile money across Africa.",
}


def _company(request):
    company = request.active_company
    if company is None:
        raise Http404("Choose a company first.")
    return company


def _gateway(company, provider):
    provider = provider.upper()
    if provider not in PROVIDERS:
        raise Http404
    found, _ = PaymentGateway.objects.get_or_create(company=company, provider=provider)
    return found


@login_required
def home(request):
    company = _company(request)
    have = {g.provider: g for g in PaymentGateway.objects.filter(company=company)}
    cards = [{"provider": p, "label": label, "gateway": have.get(p), "blurb": BLURB[p]}
             for p, label in PaymentGateway.PROVIDERS]
    return render(request, "payments/home.html", {
        "cards": cards,
        "requests": PaymentRequest.objects.filter(company=company).select_related("gateway", "invoice")[:50],
        "paybill": PaybillReceipt.objects.filter(company=company).select_related("invoice")[:30],
    })


@login_required
def gateway_edit(request, provider):
    company = _company(request)
    gw = _gateway(company, provider)
    form = GatewayForm(request.POST or None, instance=gw, company=company)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"{gw.get_provider_display()} saved.")
        return redirect("payments:home")
    base = gw.callback_base.rstrip("/") if gw.callback_base else request.build_absolute_uri("/").rstrip("/")
    return render(request, "payments/gateway_form.html", {
        "form": form, "gateway": gw,
        "c2b_confirm": base + reverse("payments:c2b_confirm", args=[gw.webhook_key]),
        "c2b_validate": base + reverse("payments:c2b_validate", args=[gw.webhook_key]),
        "ipn": base + reverse("payments:pesapal_ipn", args=[gw.webhook_key]),
    })


@login_required
@require_POST
def gateway_check(request, provider):
    company = _company(request)
    gw = _gateway(company, provider)
    try:
        note, ok = driver(gw).check(), True
    except GatewayError as exc:
        note, ok = str(exc), False
    gw.last_check, gw.last_check_ok, gw.last_check_note = timezone.now(), ok, note[:255]
    gw.save(update_fields=["last_check", "last_check_ok", "last_check_note", "updated_at"])
    (messages.success if ok else messages.error)(request, note)
    return redirect("payments:gateway_edit", provider=gw.provider.lower())


@login_required
@require_POST
def gateway_register_c2b(request, provider):
    company = _company(request)
    gw = _gateway(company, provider)
    if gw.provider != "MPESA":
        raise Http404
    base = gw.callback_base.rstrip("/") if gw.callback_base else request.build_absolute_uri("/").rstrip("/")
    if gw.environment == "SIMULATOR":
        messages.info(request, "Simulator: nothing was registered. Switch to Sandbox or Live with your Daraja keys, "
                               "then register again so Safaricom tells Mofisi about paybill payments.")
        return redirect("payments:gateway_edit", provider=gw.provider.lower())
    try:
        driver(gw).register_c2b(base + reverse("payments:c2b_confirm", args=[gw.webhook_key]),
                                base + reverse("payments:c2b_validate", args=[gw.webhook_key]))
        messages.success(request, "Daraja will now tell Mofisi about every payment to this paybill / till.")
    except GatewayError as exc:
        messages.error(request, str(exc))
    return redirect("payments:gateway_edit", provider=gw.provider.lower())


# ───────────────────────────── asking for money ─────────────────────────────

@login_required
@require_POST
def invoice_request(request, pk):
    from apps.sales.models import Invoice
    company = _company(request)
    invoice = get_object_or_404(Invoice, pk=pk, company=company)
    provider = (request.POST.get("provider") or "").upper()
    try:
        req = services.request_payment(
            company, provider, request.POST.get("amount") or invoice.outstanding_amount, user=request.user,
            request=request, invoice=invoice, phone=request.POST.get("phone", ""),
            email=request.POST.get("email", "") or getattr(invoice.customer, "email", ""), payer=invoice.customer.name)
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect("sales:invoice_detail", pk=invoice.pk)
    if provider == "MPESA":
        messages.success(request, f"M-Pesa prompt for {req.currency} {req.amount} sent to {req.phone}. "
                                  "The receipt is booked as soon as the customer enters their PIN.")
    else:
        messages.success(request, f"Payment link ready: {req.checkout_url} — send it to the customer. "
                                  "The receipt is booked when they pay.")
    return redirect("sales:invoice_detail", pk=invoice.pk)


@login_required
@require_POST
def pos_mpesa_start(request):
    """The till asks for an M-Pesa prompt for the ticket it is about to ring."""
    from apps.pos import services as pos
    company = _company(request)
    session = pos.active_session(company, request.user)
    if session is None:
        return JsonResponse({"ok": False, "error": "Open the till first."}, status=400)
    try:
        req = services.request_payment(company, "MPESA", request.POST.get("amount"), user=request.user,
                                       request=request, pos_session=session,
                                       reference=request.POST.get("reference") or f"TILL-{session.pk.hex[:6].upper()}",
                                       description="Till ticket", phone=request.POST.get("phone", ""))
    except ValidationError as exc:
        return JsonResponse({"ok": False, "error": exc.messages[0]}, status=400)
    return JsonResponse({"ok": True, "id": str(req.pk), "simulated": req.gateway.environment == "SIMULATOR",
                         "status_url": reverse("payments:request_status", args=[req.pk])})


@login_required
def request_status(request, pk):
    """Where a request stands (asks the provider while it is pending)."""
    req = get_object_or_404(PaymentRequest, pk=pk, company=_company(request))
    if request.method == "POST" and request.POST.get("simulate") and req.gateway.environment == "SIMULATOR":
        services.simulate(req, request.POST["simulate"].upper())
    else:
        services.refresh(req)
    return JsonResponse({"status": req.status, "label": req.get_status_display(), "receipt": req.receipt,
                         "amount": str(req.amount), "phone": req.phone, "note": req.note,
                         "payment": req.customer_payment.reference if req.customer_payment_id else ""})


# ───────────────────────────── gateways calling back ─────────────────────────

def _body(request):
    try:
        return json.loads(request.body or b"{}")
    except ValueError:
        return {}


# The gateways call back with a key and no signed-in company: find_anywhere
# looks in the shared database, then in each company's own database, and the
# rest of the request runs against the one the key was found in.

@csrf_exempt
@require_POST
def callback(request, provider, key):
    """M-Pesa's STK result. Its word only prompts a status query."""
    req = find_anywhere(lambda: PaymentRequest.objects.filter(callback_key=key).select_related("gateway").first())
    if req is None:
        return JsonResponse({"ResultCode": 1, "ResultDesc": "Unknown request"}, status=404)
    hint = stk_callback_items(_body(request)) if provider == "mpesa" else _body(request)
    if provider == "mpesa" and hint.get("checkout_id") and hint["checkout_id"] != req.provider_ref:
        return JsonResponse({"ResultCode": 1, "ResultDesc": "Mismatched request"}, status=400)
    services.refresh(req, hint=hint)
    return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})


@csrf_exempt
@require_GET
def pesapal_ipn(request, key):
    gw = find_anywhere(lambda: PaymentGateway.objects.filter(webhook_key=key, provider="PESAPAL").first())
    tracking = request.GET.get("OrderTrackingId", "")
    merchant = request.GET.get("OrderMerchantReference", "")
    if gw is None:
        raise Http404
    req = PaymentRequest.objects.filter(gateway=gw, provider_ref=tracking).first()
    if req is not None:
        services.refresh(req)
    return JsonResponse({"orderNotificationType": request.GET.get("OrderNotificationType", "IPNCHANGE"),
                         "orderTrackingId": tracking, "orderMerchantReference": merchant,
                         "status": 200 if req is not None else 500})


@require_GET
def returned(request, key):
    """Where Pesapal / DPO send the customer back after paying."""
    req = find_anywhere(lambda: PaymentRequest.objects.filter(callback_key=key).select_related("gateway", "company").first())
    if req is None:
        raise Http404
    services.refresh(req)
    return render(request, "payments/returned.html", {"req": req})


@csrf_exempt
@require_POST
def c2b_validate(request, key):
    if find_anywhere(lambda: PaymentGateway.objects.filter(webhook_key=key, provider="MPESA").first()) is None:
        raise Http404
    return JsonResponse({"ResultCode": "0", "ResultDesc": "Accepted"})


@csrf_exempt
@require_POST
def c2b_confirm(request, key):
    gw = find_anywhere(lambda: PaymentGateway.objects.filter(webhook_key=key, provider="MPESA").first())
    if gw is None:
        raise Http404
    try:
        services.paybill_received(gw, _body(request))
    except ValidationError as exc:
        return JsonResponse({"ResultCode": 1, "ResultDesc": exc.messages[0]}, status=400)
    return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})


@csrf_exempt
def simulate(request, key):
    """The simulator's checkout page (Pesapal / DPO in SIMULATOR mode)."""
    req = find_anywhere(lambda: PaymentRequest.objects.filter(
        callback_key=key, gateway__environment="SIMULATOR").select_related("gateway").first())
    if req is None:
        raise Http404
    if request.method == "POST":
        services.simulate(req, request.POST.get("result", "PAID").upper())
        return redirect("payments:returned", key=req.callback_key)
    return render(request, "payments/simulate.html", {"req": req})
