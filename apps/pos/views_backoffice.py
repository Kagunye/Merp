"""Restaurant back office: head office's configuration of the waiter stations,
tables, menus, happy hours and floor staff, and the branch day end.

Every object is looked up inside request.active_company, so an id from another
company is a 404 and never an edit. Who may open or change each page is the
role table in apps/core/access.py (bo_*), enforced by the access middleware.
"""
import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import restaurant, services
from .forms_backoffice import (ItemForm, MenuForm, PinForm, RuleForm, SettingsForm, StaffForm,
                               StationForm, TableForm)
from .models import (POSDayEnd, POSMenu, POSMenuItem, POSMenuSection, POSPriceRule, POSStaff,
                     POSStation, POSTable)


# ─────────────────────────────────── helpers ────────────────────────────────────

def _company(request):
    company = getattr(request, "active_company", None)
    if company is None:
        raise Http404("Choose a company first.")
    return company


def _crumbs(*trail):
    out = [{"label": "Point of Sale", "url": reverse("pos:home")},
           {"label": "Back office", "url": reverse("pos:bo_home")}]
    for label, url in trail:
        out.append({"label": label, "url": url})
    return out


def _render(request, template, ctx, *trail):
    ctx.setdefault("breadcrumbs", _crumbs(*trail))
    return render(request, template, ctx)


def _save(request, form, done_message):
    """Save a valid form, turning a lost race on a unique code into a form error."""
    try:
        with transaction.atomic():
            obj = form.save()
    except IntegrityError:
        form.add_error("code" if "code" in form.fields else None,
                       "That code was taken a moment ago. Choose another.")
        return None
    messages.success(request, done_message.format(obj=obj))
    return obj


def _branch_of(company, value):
    return services.by_pk(company.branches.filter(is_active=True), value)


# ──────────────────────────────────── home ──────────────────────────────────────

@login_required
def home(request):
    company = _company(request)
    branch = services.home_branch(company, request.user)
    running = restaurant.running_rules(company, branch) if branch else []
    ctx = {
        "page_title": "Restaurant back office",
        "branch": branch,
        "running": running,
        "counts": {
            "stations": POSStation.objects.filter(company=company, is_active=True).count(),
            "tables": POSTable.objects.filter(company=company, is_active=True).count(),
            "menus": POSMenu.objects.filter(company=company, is_active=True).count(),
            "rules": POSPriceRule.objects.filter(company=company, is_active=True).count(),
            "staff": POSStaff.objects.filter(company=company, is_active=True).count(),
            "no_pin": POSStaff.objects.filter(company=company, is_active=True,
                                              pin_hash="").count(),
            "live": restaurant.live_orders(company).count(),
            "day_ends": POSDayEnd.objects.filter(company=company).count(),
        },
        "last_day_end": POSDayEnd.objects.filter(company=company).select_related(
            "branch", "closed_by").first(),
    }
    return render(request, "pos/backoffice/home.html", ctx)


# ────────────────────────────────── settings ────────────────────────────────────

@login_required
def settings_edit(request):
    company = _company(request)
    conf = restaurant.settings_for(company)
    form = SettingsForm(request.POST or None, instance=conf, company=company)
    if request.method == "POST" and form.is_valid():
        if _save(request, form, "Head office settings saved."):
            return redirect("pos:bo_settings")
    return _render(request, "pos/backoffice/form.html",
                   {"form": form, "page_title": "Head office settings",
                    "title": "Head office settings",
                    "subtitle": "One set of rules for every branch's stations, bills and day end",
                    "back_url": reverse("pos:bo_home"), "submit_label": "Save settings"},
                   ("Head office settings", None))


# ────────────────────────────────── stations ────────────────────────────────────

@login_required
def station_list(request):
    company = _company(request)
    stations = POSStation.objects.filter(company=company).select_related(
        "branch", "warehouse", "menu").annotate(
        table_count=Count("tables", filter=Q(tables__is_active=True)))
    default_menu = restaurant.settings_for(company).default_menu
    return _render(request, "pos/backoffice/station_list.html",
                   {"stations": stations, "default_menu": default_menu,
                    "page_title": "Stations"}, ("Stations", None))


@login_required
def station_edit(request, pk=None):
    company = _company(request)
    station = get_object_or_404(POSStation, pk=pk, company=company) if pk else None
    initial = {}
    if station is None:
        branch = services.home_branch(company, request.user)
        if branch:
            initial["branch"] = branch.pk
    form = StationForm(request.POST or None, instance=station, initial=initial, company=company)
    if request.method == "POST" and form.is_valid():
        if _save(request, form, "Station {obj.name} saved."):
            return redirect("pos:bo_station_list")
    title = f"Edit {station.name}" if station else "New station"
    return _render(request, "pos/backoffice/form.html",
                   {"form": form, "page_title": title, "title": title,
                    "subtitle": "A device staff work at: a waiter station, a cashier point or the bar",
                    "back_url": reverse("pos:bo_station_list"), "submit_label": "Save station"},
                   ("Stations", reverse("pos:bo_station_list")), (title, None))


# ─────────────────────────────────── tables ─────────────────────────────────────

@login_required
def table_list(request):
    company = _company(request)
    tables = POSTable.objects.filter(company=company).select_related("branch", "station").order_by(
        "branch__name", "area", "sort_order", "name")
    return _render(request, "pos/backoffice/table_list.html",
                   {"tables": tables, "page_title": "Tables"}, ("Tables", None))


@login_required
def table_edit(request, pk=None):
    company = _company(request)
    table = get_object_or_404(POSTable, pk=pk, company=company) if pk else None
    initial = {}
    if table is None:
        branch = services.home_branch(company, request.user)
        if branch:
            initial["branch"] = branch.pk
    form = TableForm(request.POST or None, instance=table, initial=initial, company=company)
    if request.method == "POST" and form.is_valid():
        if _save(request, form, "Table {obj.name} saved."):
            if request.POST.get("another"):
                return redirect("pos:bo_table_create")
            return redirect("pos:bo_table_list")
    title = f"Edit table {table.name}" if table else "New table"
    return _render(request, "pos/backoffice/form.html",
                   {"form": form, "page_title": title, "title": title,
                    "subtitle": "Where guests sit; a bill can be opened against it",
                    "back_url": reverse("pos:bo_table_list"), "submit_label": "Save table",
                    "save_another": table is None},
                   ("Tables", reverse("pos:bo_table_list")), (title, None))


# ──────────────────────────────────── menus ─────────────────────────────────────

@login_required
def menu_list(request):
    company = _company(request)
    menus = POSMenu.objects.filter(company=company).prefetch_related("branches").annotate(
        section_count=Count("sections", distinct=True),
        item_count=Count("sections__items", distinct=True),
        station_count=Count("stations", distinct=True))
    default_menu = restaurant.settings_for(company).default_menu_id
    return _render(request, "pos/backoffice/menu_list.html",
                   {"menus": menus, "default_menu_id": default_menu, "page_title": "Menus"},
                   ("Menus", None))


@login_required
def menu_edit(request, pk=None):
    company = _company(request)
    menu = get_object_or_404(POSMenu, pk=pk, company=company) if pk else None
    form = MenuForm(request.POST or None, instance=menu, company=company)
    if request.method == "POST" and form.is_valid():
        saved = _save(request, form, "Menu {obj.name} saved.")
        if saved:
            return redirect("pos:bo_menu_detail", pk=saved.pk)
    title = f"Edit {menu.name}" if menu else "New menu"
    back = reverse("pos:bo_menu_detail", args=[menu.pk]) if menu else reverse("pos:bo_menu_list")
    return _render(request, "pos/backoffice/form.html",
                   {"form": form, "page_title": title, "title": title,
                    "subtitle": "Sections and items are added on the menu's own page",
                    "back_url": back, "submit_label": "Save menu"},
                   ("Menus", reverse("pos:bo_menu_list")), (title, None))


@login_required
def menu_detail(request, pk):
    company = _company(request)
    menu = get_object_or_404(POSMenu.objects.prefetch_related("branches"), pk=pk, company=company)
    sections = list(menu.sections.prefetch_related("items__product").all())
    item_form = ItemForm(company=company)
    return _render(request, "pos/backoffice/menu_detail.html",
                   {"menu": menu, "sections": sections, "item_form": item_form,
                    "page_title": menu.name,
                    "stations": menu.stations.select_related("branch"),
                    "is_default": restaurant.settings_for(company).default_menu_id == menu.pk},
                   ("Menus", reverse("pos:bo_menu_list")), (menu.name, None))


def _sort_order(value, default=0):
    try:
        return max(0, min(int(value), 32000))
    except (TypeError, ValueError):
        return default


@login_required
@require_POST
def section_save(request, pk):
    company = _company(request)
    menu = get_object_or_404(POSMenu, pk=pk, company=company)
    back = redirect("pos:bo_menu_detail", pk=menu.pk)
    action = request.POST.get("action", "create")
    name = (request.POST.get("name") or "").strip()[:80]
    if action == "create":
        if not name:
            messages.error(request, "Give the section a name.")
            return back
        if menu.sections.filter(name__iexact=name).exists():
            messages.error(request, f"{menu.name} already has a section called {name}.")
            return back
        order = request.POST.get("sort_order")
        if order in (None, ""):
            last = menu.sections.order_by("-sort_order").first()
            order = (last.sort_order + 10) if last else 10
        POSMenuSection.objects.create(menu=menu, name=name, sort_order=_sort_order(order))
        messages.success(request, f"Section {name} added.")
        return back
    section = services.by_pk(menu.sections.all(), request.POST.get("section"))
    if section is None:
        raise Http404("No such section on this menu.")
    if action == "update":
        if not name:
            messages.error(request, "A section needs a name.")
            return back
        if menu.sections.filter(name__iexact=name).exclude(pk=section.pk).exists():
            messages.error(request, f"{menu.name} already has a section called {name}.")
            return back
        section.name = name
        section.sort_order = _sort_order(request.POST.get("sort_order"), section.sort_order)
        section.save(update_fields=["name", "sort_order"])
        messages.success(request, f"Section {name} saved.")
    elif action == "delete":
        rules = list(section.price_rules.values_list("name", flat=True))
        if rules:
            messages.error(request, f"Section {section.name} is the scope of happy hour "
                                    f"{', '.join(rules)}. Change or delete that first.")
            return back
        count = section.items.count()
        section.delete()
        messages.success(request, f"Section {section.name} deleted"
                                  + (f" with its {count} item(s)." if count else "."))
    else:
        messages.error(request, "Unknown action.")
    return back


@login_required
@require_POST
def item_save(request, pk):
    company = _company(request)
    menu = get_object_or_404(POSMenu, pk=pk, company=company)
    section = services.by_pk(menu.sections.all(), request.POST.get("section"))
    if section is None:
        messages.error(request, "Choose the section the item goes in.")
        return redirect("pos:bo_menu_detail", pk=menu.pk)
    item = None
    if request.POST.get("item"):
        item = POSMenuItem.objects.filter(section__menu=menu, pk=request.POST["item"]).first() \
            if str(request.POST["item"]).isdigit() else None
        if item is None:
            raise Http404("No such item on this menu.")
    form = ItemForm(request.POST, instance=item, company=company)
    if form.is_valid():
        saved = form.save(commit=False)
        saved.section = section
        if item is None and not request.POST.get("sort_order"):
            last = section.items.order_by("-sort_order").first()
            saved.sort_order = (last.sort_order + 10) if last else 10
        saved.save()
        messages.success(request, f"{saved.label} {'saved' if item else 'added to ' + section.name}.")
    else:
        errors = "; ".join(f"{form.fields[f].label if f in form.fields else ''}: {' '.join(e)}"
                           .lstrip(": ") for f, e in form.errors.items())
        messages.error(request, f"The item was not saved. {errors}")
    return redirect(reverse("pos:bo_menu_detail", args=[menu.pk]) + f"#section-{section.pk}")


def _company_item(company, item):
    return get_object_or_404(POSMenuItem.objects.select_related("section__menu", "product"),
                             pk=item, section__menu__company=company)


@login_required
@require_POST
def item_toggle(request, item):
    company = _company(request)
    found = _company_item(company, item)
    found.is_available = not found.is_available
    found.save(update_fields=["is_available"])
    messages.success(request, f"{found.label} is now "
                              f"{'available' if found.is_available else 'off the menu for now'}.")
    return redirect(reverse("pos:bo_menu_detail", args=[found.section.menu_id])
                    + f"#section-{found.section_id}")


@login_required
@require_POST
def item_delete(request, item):
    company = _company(request)
    found = _company_item(company, item)
    menu_id, section_id, label = found.section.menu_id, found.section_id, found.label
    found.delete()
    messages.success(request, f"{label} removed from the menu.")
    return redirect(reverse("pos:bo_menu_detail", args=[menu_id]) + f"#section-{section_id}")


# ───────────────────────────────── happy hours ──────────────────────────────────

@login_required
def rule_list(request):
    company = _company(request)
    branch = services.home_branch(company, request.user)
    now = timezone.now()
    branch_ids = [branch.pk] if branch else list(
        company.branches.filter(is_active=True).values_list("pk", flat=True))
    rules = list(POSPriceRule.objects.filter(company=company).select_related(
        "section__menu", "category", "product").prefetch_related("branches"))
    for rule in rules:
        rule.running_now = rule.is_active and any(
            restaurant.rule_runs(rule, b, now) for b in branch_ids)
    return _render(request, "pos/backoffice/rule_list.html",
                   {"rules": rules, "branch": branch, "page_title": "Happy hours"},
                   ("Happy hours", None))


@login_required
def rule_edit(request, pk=None):
    company = _company(request)
    rule = get_object_or_404(POSPriceRule, pk=pk, company=company) if pk else None
    form = RuleForm(request.POST or None, instance=rule, company=company)
    if request.method == "POST" and form.is_valid():
        if _save(request, form, "Happy hour {obj.name} saved."):
            return redirect("pos:bo_rule_list")
    title = f"Edit {rule.name}" if rule else "New happy hour"
    selected_category = (form["category"].value() or "") if form.is_bound else (
        str(rule.category_id) if rule and rule.category_id else "")
    return _render(request, "pos/backoffice/rule_form.html",
                   {"form": form, "page_title": title, "title": title, "rule": rule,
                    "selected_category": str(selected_category)},
                   ("Happy hours", reverse("pos:bo_rule_list")), (title, None))


# ──────────────────────────────────── staff ─────────────────────────────────────

@login_required
def staff_list(request):
    company = _company(request)
    staff = POSStaff.objects.filter(company=company).select_related("branch", "user")
    role = (request.GET.get("role") or "").upper()
    if role in dict(POSStaff.ROLES):
        staff = staff.filter(role=role)
    return _render(request, "pos/backoffice/staff_list.html",
                   {"staff": staff, "roles": POSStaff.ROLES, "active_role": role,
                    "now": timezone.now(), "page_title": "Staff & PINs"},
                   ("Staff & PINs", None))


@login_required
def staff_edit(request, pk=None):
    company = _company(request)
    person = get_object_or_404(POSStaff, pk=pk, company=company) if pk else None
    form = StaffForm(request.POST or None, instance=person, company=company)
    if request.method == "POST" and form.is_valid():
        saved = _save(request, form, "{obj.name} saved.")
        if saved:
            if person is None:
                messages.info(request, f"Now set a PIN for {saved.name}: without one they "
                                       "cannot sign in at a station.")
            return redirect("pos:bo_staff_edit", pk=saved.pk)
    title = f"Edit {person.name}" if person else "New member of staff"
    return _render(request, "pos/backoffice/staff_form.html",
                   {"form": form, "page_title": title, "title": title, "person": person,
                    "pin_form": PinForm() if person else None, "now": timezone.now()},
                   ("Staff & PINs", reverse("pos:bo_staff_list")), (title, None))


@login_required
@require_POST
def staff_pin(request, pk):
    company = _company(request)
    person = get_object_or_404(POSStaff, pk=pk, company=company)
    back = redirect("pos:bo_staff_edit", pk=person.pk)
    if request.POST.get("action") == "unlock":
        POSStaff.objects.filter(pk=person.pk).update(failed_pins=0, locked_until=None,
                                                     updated_at=timezone.now())
        messages.success(request, f"{person.name} is unlocked and may try their PIN again.")
        return back
    form = PinForm(request.POST)
    if not form.is_valid():
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
        return back
    try:
        restaurant.set_pin(person, form.cleaned_data["pin"])
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return back
    messages.success(request, f"New PIN set for {person.name}.")
    return back


# ─────────────────────────────────── day end ────────────────────────────────────

def _parse_day(value):
    try:
        return datetime.date.fromisoformat((value or "").strip())
    except ValueError:
        return None


@login_required
def day_end(request):
    company = _company(request)
    branches = company.branches.filter(is_active=True).order_by("name")
    branch = _branch_of(company, request.GET.get("branch")) if request.GET.get("branch") \
        else services.home_branch(company, request.user)
    if request.GET.get("branch") and branch is None:
        raise Http404("No such branch.")
    day = _parse_day(request.GET.get("date")) or timezone.localdate()
    report = restaurant.day_report(company, branch, day) if branch else None
    history = POSDayEnd.objects.filter(company=company).select_related(
        "branch", "closed_by")[:30]
    title = "Day end"
    return _render(request, "pos/backoffice/day_end.html",
                   {"report": report, "branch": branch, "branches": branches, "day": day,
                    "today": timezone.localdate(), "history": history, "page_title": title},
                   ("Day end", None))


@login_required
@require_POST
def day_end_close(request):
    company = _company(request)
    branch = _branch_of(company, request.POST.get("branch"))
    day = _parse_day(request.POST.get("date"))
    if branch is None or day is None:
        messages.error(request, "Choose the branch and the day to close.")
        return redirect("pos:bo_day_end")
    back = redirect(reverse("pos:bo_day_end") + f"?branch={branch.pk}&date={day.isoformat()}")
    try:
        restaurant.close_day(company, branch, day, request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return back
    messages.success(request, f"{day:%d %b %Y} at {branch.name} is closed.")
    return back


# ─────────────────────────────── printers & drawers ──────────────────────────────

@login_required
def printer_list(request):
    from .hardware import printer_config
    from .models import POSDrawerEvent, POSPrinter
    company = _company(request)
    printers = list(POSPrinter.objects.filter(company=company).select_related("branch"))
    configs = [printer_config(p) for p in printers if p.is_active]
    events = POSDrawerEvent.objects.filter(company=company).select_related("user", "printer", "sale")[:25]
    return _render(request, "pos/backoffice/printer_list.html",
                   {"printers": printers, "printer_configs": configs, "events": events,
                    "page_title": "Printers & cash drawers"}, ("Printers & drawers", None))


@login_required
def printer_edit(request, pk=None):
    from .forms_backoffice import PrinterForm
    from .models import POSPrinter
    company = _company(request)
    printer = get_object_or_404(POSPrinter, pk=pk, company=company) if pk else None
    form = PrinterForm(request.POST or None, instance=printer, company=company)
    if request.method == "POST" and form.is_valid():
        if _save(request, form, "Printer {obj.name} saved."):
            return redirect("pos:bo_printer_list")
    title = f"Edit {printer.name}" if printer else "New printer / cash drawer"
    return _render(request, "pos/backoffice/form.html",
                   {"form": form, "page_title": title, "title": title,
                    "subtitle": "A receipt printer on the shop network, and the cash drawer plugged into it",
                    "back_url": reverse("pos:bo_printer_list"), "submit_label": "Save printer"},
                   ("Printers & drawers", reverse("pos:bo_printer_list")), (title, None))


@login_required
@require_POST
def item_picture(request, item):
    """Upload (or remove) the picture a menu item's product shows on the tills."""
    from . import hardware
    from .models import ProductPicture
    company = _company(request)
    found = _company_item(company, item)
    back = reverse("pos:bo_menu_detail", args=[found.section.menu_id]) + f"#section-{found.section_id}"
    if request.POST.get("action") == "remove":
        ProductPicture.objects.filter(product=found.product).delete()
        messages.success(request, f"Picture removed from {found.label}.")
        return redirect(back)
    try:
        hardware.save_picture(found.product, request.FILES.get("picture"))
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect(back)
    messages.success(request, f"Picture saved for {found.label}; the tills show it from the next screen.")
    return redirect(back)
