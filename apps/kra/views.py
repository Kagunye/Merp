"""KRA eTIMS screens: settings, devices, codes, items, the outbox, supplier
invoices, imports, PIN lookup, notices and the readiness checklist.

Access is decided by RoleAccessMiddleware (apps/core/access.py); every object
here is scoped to the active company."""
import json
import os

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import services
from .forms import EtimsDeviceForm, EtimsItemForm, EtimsSettingsForm, PinCheckForm
from .models import (EtimsCode, EtimsDevice, EtimsImport, EtimsItem, EtimsItemClass, EtimsNotice,
                     EtimsPurchase, EtimsReceipt, EtimsSettings, EtimsSubmission, EtimsSyncState,
                     EtimsTaxMap)

PAGE_SIZE = 50
TAX_LETTERS = EtimsTaxMap.TAX_TYPES


# ─────────────────────────────────── helpers ───────────────────────────────────

def _company(request):
    return request.active_company


def _err(exc):
    return " ".join(getattr(exc, "messages", None) or [str(exc)])


def _page(request, qs, size=PAGE_SIZE):
    page_obj = Paginator(qs, size).get_page(request.GET.get("page"))
    return {"page_obj": page_obj, "is_paginated": page_obj.has_other_pages(),
            "object_list": page_obj.object_list}


def _crumbs(*items):
    out = [{"label": "KRA eTIMS", "url": reverse("kra:home")}]
    out += [{"label": label, "url": url} for label, url in items]
    return out


def _ready_devices(company):
    return [d for d in EtimsDevice.objects.filter(company=company, is_active=True) if d.ready]


def _report_process(request, stats, prefix=""):
    if not stats:
        return
    parts = [f"{stats.get('sent', 0)} sent"]
    if stats.get("failed"):
        parts.append(f"{stats['failed']} refused")
    if stats.get("held"):
        parts.append(f"{stats['held']} waiting")
    if stats.get("transport"):
        parts.append("KRA could not be reached — the rest stays queued")
    level = messages.warning if stats.get("failed") or stats.get("transport") else messages.success
    level(request, (prefix + "Queue: " + ", ".join(parts) + ".").strip())


def _back(request, default):
    nxt = request.POST.get("next") or ""
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else default


# ──────────────────────────────────── home ─────────────────────────────────────

def home(request):
    company = _company(request)
    conf = services.settings_for(company)
    devices = list(EtimsDevice.objects.filter(company=company).select_related("branch"))
    syncs = {row["device"]: row["last"] for row in EtimsSyncState.objects.filter(
        device__company=company).values("device").annotate(last=Max("synced_at"))}
    for d in devices:
        d.last_sync = syncs.get(d.pk)
    counts = services.queue_counts(company)
    statuses = [(code, label, counts.get(code, 0)) for code, label in EtimsSubmission.STATUSES]
    ready = sum(1 for d in devices if d.ready and d.is_active)
    active = sum(1 for d in devices if d.is_active)
    warnings = []
    if not conf.enabled:
        warnings.append("eTIMS is off: no invoice or till sale is sent to KRA, and nothing is queued.")
    if not devices:
        warnings.append("No device yet: add one for head office (bhfId 00) and initialise it.")
    elif ready < active:
        warnings.append(f"{active - ready} device(s) not initialised: their documents wait in the queue.")
    if counts.get("FAILED"):
        warnings.append(f"{counts['FAILED']} submission(s) were refused by KRA and need attention; "
                        "a refused sale holds every later sale of its device.")
    if not conf.kra_pin:
        warnings.append("No KRA PIN: set it in eTIMS settings or on the company record.")
    return render(request, "kra/home.html", {
        "conf": conf, "devices": devices, "ready_count": ready, "active_count": active,
        "statuses": statuses, "counts": counts, "today": services.totals_today(company),
        "warnings": warnings, "cron_token_set": bool(os.environ.get("KRA_CRON_TOKEN")),
        "recent_receipts": EtimsReceipt.objects.filter(company=company).select_related("device", "submission")[:8],
    })


# ────────────────────────────────── settings ───────────────────────────────────

def settings_edit(request):
    company = _company(request)
    conf = services.settings_for(company)
    form = EtimsSettingsForm(request.POST or None, instance=conf)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "eTIMS settings saved.")
        if form.instance.enabled and not EtimsDevice.objects.filter(company=company).exists():
            messages.info(request, "Next: add a device for head office and initialise it.")
        return redirect("kra:home")
    return render(request, "kra/settings_form.html", {
        "form": form, "conf": conf,
        "breadcrumbs": _crumbs(("Settings", "")),
    })


def tax_map(request):
    company = _company(request)
    from apps.accounting.models import TaxRate
    rates = list(TaxRate.objects.filter(company=company).order_by("rate", "name"))
    maps = {m.tax_rate_id: m.tax_type for m in EtimsTaxMap.objects.filter(tax_rate__in=rates)}
    valid = {k for k, _ in TAX_LETTERS}
    if request.method == "POST":
        saved = 0
        for rate in rates:
            letter = request.POST.get(f"tax_{rate.pk}", "")
            if letter not in valid:
                continue
            EtimsTaxMap.objects.update_or_create(tax_rate=rate, defaults={"company": company,
                                                                          "tax_type": letter})
            saved += 1
        messages.success(request, f"KRA tax types saved for {saved} tax rate(s).")
        return redirect("kra:settings_tax_map")
    rows = []
    for rate in rates:
        default = None
        try:
            default = services.tax_type(company, rate) if rate.pk not in maps else None
        except ValidationError:
            default = None
        rows.append({"rate": rate, "mapped": maps.get(rate.pk), "current": maps.get(rate.pk) or default,
                     "default": default})
    return render(request, "kra/tax_map.html", {
        "rows": rows, "letters": TAX_LETTERS,
        "breadcrumbs": _crumbs(("Settings", reverse("kra:settings_edit")), ("Tax types", "")),
    })


# ─────────────────────────────────── devices ───────────────────────────────────

def device_list(request):
    company = _company(request)
    devices = EtimsDevice.objects.filter(company=company).select_related("branch")
    return render(request, "kra/device_list.html", {
        "devices": devices, "conf": services.settings_for(company),
        "breadcrumbs": _crumbs(("Devices", "")),
    })


def device_edit(request, pk=None):
    company = _company(request)
    device = get_object_or_404(EtimsDevice, pk=pk, company=company) if pk else EtimsDevice(company=company)
    form = EtimsDeviceForm(request.POST or None, instance=device, company=company)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.company = company
        obj.save()
        messages.success(request, f"Device for {obj.branch} saved." +
                         ("" if obj.ready else " Initialise it to get its key from KRA."))
        return redirect("kra:device_list")
    return render(request, "kra/device_form.html", {
        "form": form, "device": device if pk else None,
        "breadcrumbs": _crumbs(("Devices", reverse("kra:device_list")),
                               (str(device) if pk else "New device", "")),
    })


@require_POST
def device_init(request, pk):
    device = get_object_or_404(EtimsDevice, pk=pk, company=_company(request))
    try:
        services.initialize_device(device)
    except ValidationError as exc:
        messages.error(request, _err(exc))
    else:
        messages.success(request, f"Initialised: SCU {device.sdc_id}, MRC {device.mrc_no}. "
                                  f"KRA knows this branch as {device.branch_name or device.bhf_id}.")
    return redirect(_back(request, reverse("kra:device_list")))


# ──────────────────────────────── codes & notices ──────────────────────────────

def codes(request):
    company = _company(request)
    classes = list(EtimsCode.objects.filter(company=company).values("cd_cls").annotate(
        n=Count("id"), name=Max("cd_cls_name")).order_by("cd_cls"))
    item_class_count = EtimsItemClass.objects.filter(company=company).count()
    tab = request.GET.get("cls") or (classes[0]["cd_cls"] if classes else ("items" if item_class_count else ""))
    q = (request.GET.get("q") or "").strip()
    ctx = {"classes": classes, "tab": tab, "q": q, "item_class_count": item_class_count,
           "breadcrumbs": _crumbs(("Codes", ""))}
    if tab == "items":
        qs = EtimsItemClass.objects.filter(company=company)
        if q:
            qs = qs.filter(Q(code__icontains=q) | Q(name__icontains=q))
        ctx.update(_page(request, qs))
    elif tab:
        qs = EtimsCode.objects.filter(company=company, cd_cls=tab)
        if q:
            qs = qs.filter(Q(cd__icontains=q) | Q(name__icontains=q))
        ctx["codes"] = qs
        ctx["tab_name"] = next((c["name"] for c in classes if c["cd_cls"] == tab), tab)
    syncs = EtimsSyncState.objects.filter(device__company=company, kind="CODES").aggregate(m=Max("synced_at"))
    ctx["last_sync"] = syncs["m"]
    return render(request, "kra/codes.html", ctx)


@require_POST
def codes_sync(request):
    company = _company(request)
    devices = _ready_devices(company)
    if not devices:
        messages.error(request, "Initialise an eTIMS device first.")
        return redirect(_back(request, reverse("kra:codes")))
    for device in devices:
        try:
            n_codes = services.sync_codes(device)
            n_cls = services.sync_item_classes(device)
            n_notes = services.sync_notices(device)
        except ValidationError as exc:
            messages.error(request, f"bhfId {device.bhf_id}: {_err(exc)}")
            continue
        messages.success(request, f"bhfId {device.bhf_id}: {n_codes} codes, {n_cls} item classes and "
                                  f"{n_notes} notices updated.")
    return redirect(_back(request, reverse("kra:codes")))


def notices(request):
    company = _company(request)
    ctx = _page(request, EtimsNotice.objects.filter(company=company))
    ctx["breadcrumbs"] = _crumbs(("Notices", ""))
    return render(request, "kra/notices.html", ctx)


# ──────────────────────────────────── items ────────────────────────────────────

def _letter_for(company, rate, cache):
    key = rate.pk if rate else None
    if key not in cache:
        try:
            cache[key] = services.tax_type(company, rate)
        except ValidationError:
            cache[key] = ""
    return cache[key]


def items(request):
    company = _company(request)
    from apps.inventory.models import Product
    qs = Product.objects.filter(company=company, is_active=True).select_related(
        "tax_rate", "unit_of_measure", "etims_item").order_by("name")
    q = (request.GET.get("q") or "").strip()
    state = request.GET.get("state") or ""
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(sku__icontains=q) | Q(etims_item__item_cd__icontains=q))
    if state == "registered":
        qs = qs.filter(etims_item__registered_at__isnull=False)
    elif state == "unregistered":
        qs = qs.filter(Q(etims_item__isnull=True) | Q(etims_item__registered_at__isnull=True))
    ctx = _page(request, qs)
    cache = {}
    rows = []
    for product in ctx["object_list"]:
        item = getattr(product, "etims_item", None)
        rows.append({"product": product, "item": item, "letter": _letter_for(company, product.tax_rate, cache)})
    unregistered = Product.objects.filter(company=company, is_active=True).filter(
        Q(etims_item__isnull=True) | Q(etims_item__registered_at__isnull=True)).count()
    ctx.update({
        "rows": rows, "q": q, "state": state, "unregistered": unregistered,
        "item_types": EtimsItem.ITEM_TYPES,
        "qty_units": list(EtimsCode.objects.filter(company=company, cd_cls="10", use_yn=True)),
        "pkg_units": list(EtimsCode.objects.filter(company=company, cd_cls="17", use_yn=True)),
        "item_classes": list(EtimsItemClass.objects.filter(company=company, use_yn=True)[:500]),
        "conf": services.settings_for(company), "has_device": services.device_for(company) is not None,
        "breadcrumbs": _crumbs(("Items", "")),
    })
    return render(request, "kra/items.html", ctx)


@require_POST
def item_register(request):
    company = _company(request)
    from apps.inventory.models import Product
    back = _back(request, reverse("kra:items"))
    action = request.POST.get("action") or "register"
    if action == "save":
        product = get_object_or_404(Product, pk=request.POST.get("product"), company=company)
        try:
            item = services.item_for(product)
        except ValidationError as exc:
            messages.error(request, _err(exc))
            return redirect(back)
        form = EtimsItemForm(request.POST, instance=item)
        if not form.is_valid():
            errors = "; ".join(f"{form.fields[k].label}: {' '.join(v)}" for k, v in form.errors.items())
            messages.error(request, f"{product.name}: {errors}")
            return redirect(back)
        form.save()
        msg = f"{product.name}: KRA classes and units saved."
        if item.registered_at:
            try:
                if services.queue_item(product, request.user):
                    _report_process(request, services.process(company))
                    msg += " Sent to KRA again because what KRA holds changed."
            except ValidationError as exc:
                messages.error(request, _err(exc))
        messages.success(request, msg)
        return redirect(back)

    conf = services.settings_for(company)
    if not conf.enabled:
        messages.error(request, "Turn eTIMS on in settings first.")
        return redirect(back)
    if services.device_for(company) is None:
        messages.error(request, "Add and initialise an eTIMS device first.")
        return redirect(back)
    products = Product.objects.filter(company=company, is_active=True)
    if action == "register_all":
        products = products.filter(Q(etims_item__isnull=True) | Q(etims_item__registered_at__isnull=True))
    else:
        ids = request.POST.getlist("products")
        if not ids:
            messages.warning(request, "Tick the products to register first.")
            return redirect(back)
        products = products.filter(pk__in=ids)
    queued, problems = 0, []
    for product in products.select_related("tax_rate", "unit_of_measure"):
        try:
            if services.queue_item(product, request.user, force=True):
                queued += 1
        except ValidationError as exc:
            problems.append(f"{product.name}: {_err(exc)}")
    if problems:
        messages.error(request, "Not queued — " + " | ".join(problems[:5]) +
                       (f" (and {len(problems) - 5} more)" if len(problems) > 5 else ""))
    if queued:
        messages.info(request, f"{queued} item(s) queued for KRA.")
        _report_process(request, services.process(company))
    elif not problems:
        messages.info(request, "Nothing to register.")
    return redirect(back)


# ──────────────────────────────────── queue ────────────────────────────────────

def queue(request):
    company = _company(request)
    qs = EtimsSubmission.objects.filter(company=company).select_related("device").order_by("-created_at")
    status = request.GET.get("status") or ""
    kind = request.GET.get("kind") or ""
    q = (request.GET.get("q") or "").strip()
    if status in dict(EtimsSubmission.STATUSES):
        qs = qs.filter(status=status)
    if kind in dict(EtimsSubmission.KINDS):
        qs = qs.filter(kind=kind)
    if q:
        filt = Q(source_label__icontains=q) | Q(result_message__icontains=q) | Q(last_error__icontains=q)
        if q.isdigit():
            filt |= Q(sequence=int(q))
        qs = qs.filter(filt)
    ctx = _page(request, qs)
    ctx.update({"status": status, "kind": kind, "q": q, "statuses": EtimsSubmission.STATUSES,
                "kinds": EtimsSubmission.KINDS, "counts": services.queue_counts(company),
                "breadcrumbs": _crumbs(("Queue", ""))})
    return render(request, "kra/queue.html", ctx)


def _pretty(value):
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def queue_detail(request, pk):
    sub = get_object_or_404(EtimsSubmission.objects.select_related("device", "depends_on"), pk=pk,
                            company=_company(request))
    receipt = EtimsReceipt.objects.filter(submission=sub).first()
    return render(request, "kra/queue_detail.html", {
        "sub": sub, "receipt": receipt, "payload_json": _pretty(sub.payload),
        "response_json": _pretty(sub.response) if sub.response else "",
        "can_retry": sub.status in ("FAILED", "HELD", "PENDING"),
        "breadcrumbs": _crumbs(("Queue", reverse("kra:queue")), (str(sub), "")),
    })


@require_POST
def queue_retry(request, pk):
    sub = get_object_or_404(EtimsSubmission, pk=pk, company=_company(request))
    try:
        stats = services.retry(sub)
    except ValidationError as exc:
        messages.error(request, _err(exc))
    else:
        sub.refresh_from_db()
        if sub.status == "SENT":
            messages.success(request, f"{sub}: accepted by KRA.")
        elif sub.status == "FAILED":
            messages.error(request, f"{sub}: refused again — {sub.last_error or sub.result_message}")
        else:
            messages.warning(request, f"{sub}: {sub.get_status_display().lower()}.")
        _report_process(request, stats)
    return redirect(_back(request, reverse("kra:queue_detail", args=[sub.pk])))


@require_POST
def queue_run(request):
    company = _company(request)
    if not services.settings_for(company).enabled:
        messages.error(request, "eTIMS is off: turn it on in settings to send the queue.")
    else:
        stats = services.process(company)
        if not any(stats.values()):
            messages.info(request, "Nothing was due to send.")
        else:
            _report_process(request, stats)
    return redirect(_back(request, reverse("kra:queue")))


# ───────────────────────────────── purchases ───────────────────────────────────

def purchases(request):
    company = _company(request)
    qs = EtimsPurchase.objects.filter(company=company).select_related("device")
    status = request.GET.get("status") or ""
    q = (request.GET.get("q") or "").strip()
    if status in dict(EtimsPurchase.STATUSES):
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(Q(supplier_name__icontains=q) | Q(supplier_tin__icontains=q) |
                       Q(supplier_invoice_no__icontains=q))
    ctx = _page(request, qs)
    ctx.update({"status": status, "q": q, "statuses": EtimsPurchase.STATUSES,
                "to_review": EtimsPurchase.objects.filter(company=company, status="FETCHED").count(),
                "breadcrumbs": _crumbs(("Supplier invoices", ""))})
    return render(request, "kra/purchases.html", ctx)


@require_POST
def purchases_fetch(request):
    company = _company(request)
    devices = _ready_devices(company)
    if not devices:
        messages.error(request, "Initialise an eTIMS device first.")
    for device in devices:
        try:
            n = services.fetch_purchases(device)
        except ValidationError as exc:
            messages.error(request, f"bhfId {device.bhf_id}: {_err(exc)}")
        else:
            messages.success(request, f"bhfId {device.bhf_id}: {n} new supplier invoice(s) from KRA.")
    return redirect(_back(request, reverse("kra:purchases")))


@require_POST
def purchase_decide(request, pk):
    company = _company(request)
    purchase = get_object_or_404(EtimsPurchase, pk=pk, company=company)
    action = request.POST.get("action")
    if action not in ("confirm", "reject"):
        messages.error(request, "Choose confirm or reject.")
        return redirect("kra:purchases")
    try:
        services.confirm_purchase(purchase, request.user, accept=(action == "confirm"))
    except ValidationError as exc:
        messages.error(request, _err(exc))
        return redirect(_back(request, reverse("kra:purchases")))
    services.process(company)
    purchase.refresh_from_db()
    if purchase.status == "FETCHED":
        messages.warning(request, f"{purchase.supplier_name} #{purchase.supplier_invoice_no}: "
                                  "your answer is queued; KRA has not accepted it yet (see the queue).")
    else:
        messages.success(request, f"{purchase.supplier_name} #{purchase.supplier_invoice_no}: "
                                  f"{purchase.get_status_display().lower()} with KRA.")
    return redirect(_back(request, reverse("kra:purchases")))


# ─────────────────────────────────── imports ───────────────────────────────────

def imports(request):
    company = _company(request)
    from apps.inventory.models import Product
    qs = EtimsImport.objects.filter(company=company).select_related("device", "product")
    status = request.GET.get("status") or ""
    if status in dict(EtimsImport.STATUSES):
        qs = qs.filter(status=status)
    ctx = _page(request, qs)
    waiting = EtimsImport.objects.filter(company=company, status="2").count()
    ctx.update({"status": status, "statuses": EtimsImport.STATUSES, "waiting": waiting,
                "products": Product.objects.filter(company=company, is_active=True).only(
                    "id", "name", "sku").order_by("name") if waiting else [],
                "breadcrumbs": _crumbs(("Imports", ""))})
    return render(request, "kra/imports.html", ctx)


@require_POST
def imports_fetch(request):
    company = _company(request)
    devices = _ready_devices(company)
    if not devices:
        messages.error(request, "Initialise an eTIMS device first.")
    for device in devices:
        try:
            n = services.fetch_imports(device)
        except ValidationError as exc:
            messages.error(request, f"bhfId {device.bhf_id}: {_err(exc)}")
        else:
            messages.success(request, f"bhfId {device.bhf_id}: {n} new import item(s) from KRA.")
    return redirect(_back(request, reverse("kra:imports")))


@require_POST
def import_decide(request, pk):
    company = _company(request)
    from apps.inventory.models import Product
    record = get_object_or_404(EtimsImport, pk=pk, company=company)
    action = request.POST.get("action")
    if action not in ("approve", "reject"):
        messages.error(request, "Choose approve or reject.")
        return redirect("kra:imports")
    product = None
    if request.POST.get("product"):
        product = get_object_or_404(Product, pk=request.POST.get("product"), company=company)
    try:
        services.decide_import(record, request.user, approve=(action == "approve"),
                               product=product if action == "approve" else None)
    except ValidationError as exc:
        messages.error(request, _err(exc))
        return redirect(_back(request, reverse("kra:imports")))
    services.process(company)
    record.refresh_from_db()
    if record.status == "2":
        messages.warning(request, f"{record.item_name}: your decision is queued; KRA has not accepted it yet.")
    else:
        messages.success(request, f"{record.item_name}: {record.get_status_display().lower()} with KRA.")
    return redirect(_back(request, reverse("kra:imports")))


# ───────────────────────────────── PIN lookup ──────────────────────────────────

def pin_check(request):
    company = _company(request)
    form = PinCheckForm(request.POST or None)
    result, looked_up, error = None, False, ""
    if request.method == "POST" and form.is_valid():
        try:
            result = services.lookup_pin(company, form.cleaned_data["pin"])
            looked_up = True
        except ValidationError as exc:
            error = _err(exc)
    return render(request, "kra/pin_check.html", {
        "form": form, "result": result, "looked_up": looked_up, "error": error,
        "breadcrumbs": _crumbs(("PIN check", "")),
    })


# ────────────────────────────────── checklist ──────────────────────────────────

def checklist(request):
    from . import selftest
    company = _company(request)
    conf = services.settings_for(company)
    can_live = conf.environment == "SANDBOX" and conf.backend != "SIMULATOR"
    report, error = None, ""
    if request.method == "POST":
        live = request.POST.get("mode") == "live"
        if live and not can_live:
            error = "The live run needs the sandbox environment and a real backend (OSCU, VSCU or Pergamon)."
        else:
            try:
                report = selftest.run(company, user=request.user, live=live)
            except ValidationError as exc:
                error = _err(exc)
    groups = []
    if report:
        by_group = {}
        for r in report["results"]:
            by_group.setdefault(r["group"], []).append(r)
        for gid, title in report["groups"]:
            rows = by_group.get(gid, [])
            if rows:
                groups.append({"id": gid, "title": title, "rows": rows,
                               "passed": sum(1 for r in rows if r["ok"]), "total": len(rows)})
    return render(request, "kra/checklist.html", {
        "conf": conf, "can_live": can_live, "report": report, "groups": groups, "error": error,
        "breadcrumbs": _crumbs(("Checklist", "")),
    })


# ── Every screen needs an active company (a platform admin may have none) ──
def _company_required(view):
    from functools import wraps

    from django.contrib import messages as _messages
    from django.shortcuts import redirect as _redirect

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not getattr(request, "active_company", None):
            _messages.error(request, "Create or switch to a company before setting up KRA eTIMS.")
            return _redirect("organizations:company_create")
        return view(request, *args, **kwargs)
    return wrapped


for _name in ("home", "settings_edit", "tax_map", "device_list", "device_edit", "device_init", "codes",
              "codes_sync", "notices", "items", "item_register", "queue", "queue_detail", "queue_retry",
              "queue_run", "purchases", "purchases_fetch", "purchase_decide", "imports", "imports_fetch",
              "import_decide", "pin_check", "checklist"):
    globals()[_name] = _company_required(globals()[_name])
