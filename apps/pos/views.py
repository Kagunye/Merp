"""Point of sale views: the terminal, the till session, and the tickets."""
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST
from django.views.generic import DetailView, ListView, TemplateView, View

from apps.accounting.currency import book_currency
from apps.core.mixins import CompanyRequiredMixin

from . import services
from .models import POSCashMovement, POSSale, POSSaleLine, POSSession

# The server-side half of F7 Lock Station: while this session key names the
# till, the terminal renders behind the lock screen (a reload does not lift it)
# and refuses to ring a ticket or move the drawer's money until the cashier's
# password is given at pos:unlock.
LOCK_KEY = "pos_locked_session"

# How many products the till ships to the browser for its picture grid. A
# larger catalogue is still sold in full: the code box and the item modal
# search the server (pos:item_lookup).
GRID_LIMIT = 600


def is_locked(request, cashier_session):
    return bool(cashier_session) and request.session.get(LOCK_KEY) == str(cashier_session.pk)


class ModuleHomeView(LoginRequiredMixin, TemplateView):
    template_name = "pos/home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Point of Sale"
        company = getattr(self.request, "active_company", None)
        if company:
            ctx["open_session"] = services.active_session(company, self.request.user)
            ctx["recent_sessions"] = POSSession.objects.filter(
                company=company).select_related("cashier", "branch").order_by(
                "-opened_at")[:10]
        return ctx


class SessionListView(LoginRequiredMixin, ListView):
    model = POSSession
    template_name = "pos/session_list.html"
    context_object_name = "sessions"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return POSSession.objects.none()
        qs = POSSession.objects.filter(company=company).select_related("cashier", "branch")
        status = (self.request.GET.get("status") or "").strip().upper()
        if status in dict(POSSession.STATUS_CHOICES):
            qs = qs.filter(status=status)
        # Newest till first, and a fixed order so the pages never shuffle
        # (paginating an unordered queryset can repeat or skip a session).
        return qs.order_by("-opened_at", "-created_at")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["statuses"] = POSSession.STATUS_CHOICES
        ctx["active_status"] = (self.request.GET.get("status") or "").strip().upper()
        return ctx


@login_required
def session_open(request):
    """Start a till: the branch it trades from and the float in the drawer.

    One open session per cashier, so the day's money can always be traced to the
    person standing at it — and so a second "open" simply hands back the till that
    is already running instead of starting a parallel set of takings.
    """
    company = request.active_company
    if not company:
        messages.error(request, "Create or switch to a company before opening a till.")
        return redirect("organizations:company_create")
    existing = services.active_session(company, request.user)
    if existing:
        messages.info(
            request, f"You already have a till open at {existing.branch.name} "
            f"since {existing.opened_at:%d %b %H:%M}.")
        return redirect("pos:terminal")
    if request.method == "POST":
        from apps.organizations.models import Branch
        branch = None
        if (request.POST.get("branch") or "").strip():
            # A branch id that is not a UUID, or not this company's, is the same
            # refusal: there is no such branch to trade from.
            branch = services.by_pk(
                Branch.objects.filter(company=company, is_active=True),
                request.POST.get("branch"))
            if not branch:
                messages.error(request, "That branch does not belong to this company.")
                return redirect("pos:session_open")
        try:
            cashier_session = services.open_session(
                company, request.user, branch=branch,
                opening_cash=request.POST.get("opening_cash") or 0)
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
            return redirect("pos:session_open")
        messages.success(
            request, f"Till open at {cashier_session.branch.name} — "
            f"{cashier_session.opening_cash} float in the drawer.")
        return redirect("pos:terminal")
    return render(request, "pos/session_open.html", {
        "page_title": "Open Till",
        "branches": company.branches.filter(is_active=True).order_by("name"),
        "default_branch": services.home_branch(company, request.user),
        "book": book_currency(company),
    })


class TillView(LoginRequiredMixin, CompanyRequiredMixin, View):
    """The terminal: pick the goods, count the money, ring the ticket.

    The ticket is posted from the screen itself rather than from a client-side
    cart, which is how every other counting screen in this book works — a row per
    product, a quantity box, and the server prices what was typed. Nothing a
    cashier sees can therefore disagree with what the ledger is about to be told.
    """
    template_name = "pos/terminal.html"

    def get_context_data(self, cashier_session, company, quantities=None, discounts=None):
        from apps.crm.models import Customer
        from apps.inventory.models import StockLevel

        q = (self.request.GET.get("q") or self.request.POST.get("q") or "").strip()
        products = services.search_products(company, q)
        shelf = (services.issue_warehouse(company, cashier_session)
                 if cashier_session else None)
        on_hand = {}
        if shelf:
            on_hand = {row["product_id"]: row["quantity"] for row in StockLevel.objects.filter(
                warehouse=shelf).values("product_id", "quantity")}
        tree = services.category_tree(company) if company else {}
        cards = []
        for product in products[:GRID_LIMIT]:
            card = services.product_card(product, on_hand.get(product.pk, Decimal("0")))
            top = services.top_category(tree, product.category_id) if product.category_id else None
            card["screen"] = str(top) if top else ""
            cards.append(card)
        # The ticket a failed post came back with, so the cashier does not have
        # to scan it all again: {product id: [quantity, discount]}.
        ticket = {pk: [str(qty), str((discounts or {}).get(pk, "") or "")]
                  for pk, qty in (quantities or {}).items() if str(qty or "").strip()}
        customers = Customer.objects.filter(company=company, is_active=True).order_by("name")
        methods = [(value, label) for value, label in POSSale.PAYMENT_METHODS if value != "SPLIT"]
        report = services.till_report(cashier_session) if cashier_session else None
        user = self.request.user
        categories = [{"id": str(pk), "name": name, "parent": str(parent) if parent else ""}
                      for pk, (name, parent) in sorted(tree.items(), key=lambda kv: kv[1][0])]
        screens = [c for c in categories if not c["parent"]
                   and any(card["screen"] == c["id"] for card in cards)]
        movement_accounts = {
            direction: [{"id": str(a.pk), "label": f"{a.code} {a.name}"}
                        for a in services.movement_accounts(company, direction)]
            for direction in ("OUT", "IN")} if company else {}
        defaults = {}
        for direction in ("OUT", "IN"):
            found = services.default_movement_account(company, direction) if company else None
            defaults[direction] = str(found.pk) if found else ""
        return {
            "page_title": "Till",
            "session": cashier_session,
            "company": company,
            "book": book_currency(company),
            "products_json": cards,
            "categories_json": categories,
            "screens": screens,
            "ticket_json": ticket,
            "boms_json": self.boms(company) if cashier_session else [],
            "q": q,
            "customers": customers,
            "salespeople": services.company_people(company) if company else [],
            "methods": methods,
            "is_supervisor": services.is_supervisor(company, user) if company else False,
            "locked": is_locked(self.request, cashier_session),
            "movement_token": services.new_token(),
            "movement_accounts": movement_accounts,
            "movement_defaults": defaults,
            "posted": self.request.POST if self.request.method == "POST" else {},
            # Only the shelves this till may sell from: its own branch's and
            # any shared store, never another branch's stock.
            "warehouses": (list(services.till_warehouses(company, cashier_session))
                           if company and cashier_session else []),
            # A fresh one-time token per rendering of the form; see
            # services.submitted_ticket for why.
            "ticket_token": services.new_token(),
            "shelf": shelf,
            "report": report,
            "branches": company.branches.all() if company else [],
            "recent_sales": (POSSale.objects.filter(session=cashier_session)
                             .select_related("customer").order_by("-created_at")[:10]
                             if cashier_session else POSSale.objects.none()),
        }

    @staticmethod
    def boms(company):
        """The active bills of materials, for Load BOM: each one's
        components per one finished unit, as ticket lines. Read only -- the
        till never changes a BOM, and each component is sold (and priced)
        as itself."""
        from apps.manufacturing.models import BillOfMaterials

        found = []
        for bom in (BillOfMaterials.objects.filter(company=company, status="ACTIVE")
                    .select_related("product").prefetch_related("lines__component")
                    .order_by("name")[:100]):
            per = bom.production_quantity or Decimal("1")
            lines = [{"id": str(line.component_id), "sku": line.component.sku,
                      "name": line.component.name,
                      "qty": str((line.quantity / per).quantize(Decimal("0.0001")))}
                     for line in bom.lines.all() if line.component.is_active]
            if lines:
                found.append({"id": str(bom.pk), "name": bom.name, "version": bom.version,
                              "product": bom.product.name, "lines": lines})
        return found

    def get(self, request, *args, **kwargs):
        company = request.active_company
        cashier_session = services.active_session(company, request.user)
        return render(request, self.template_name,
                      self.get_context_data(cashier_session, company))

    def post(self, request, *args, **kwargs):
        """Ring the ticket the cashier typed and counted."""
        company = request.active_company
        cashier_session = services.active_session(company, request.user)
        if not cashier_session:
            messages.error(request, "Open the till before ringing a ticket.")
            return redirect("pos:session_open")
        quantities = {key[4:]: value for key, value in request.POST.items()
                      if key.startswith("qty_")}
        discounts = {key[5:]: value for key, value in request.POST.items()
                     if key.startswith("disc_")}
        if is_locked(request, cashier_session):
            messages.error(request, "This till is locked — unlock it with your password first. "
                                    "Nothing was rung.")
            return redirect("pos:terminal")
        token = services.clean_token(request.POST.get("ticket_token"))
        if not token:
            # The token is what makes a double click or a reloaded POST hand back
            # the ticket it already rang instead of taking the money twice. A
            # post without one — a stale page, a script, a hand-made request —
            # has nothing to be recognised by, so it is refused rather than rung
            # unguarded. The screen is shown again with a fresh token.
            messages.error(
                request,
                "That ticket form had no one-time token, so it could not be protected "
                "against being rung twice. Nothing was taken — check the lines below and "
                "ring the ticket again from this screen.")
            return render(request, self.template_name,
                          self.get_context_data(cashier_session, company, quantities, discounts),
                          status=200)
        already = services.submitted_ticket(cashier_session, token)
        if already:
            return self.answer_spent_token(request, already, cashier_session, company,
                                           quantities)
        try:
            customer = None
            if (request.POST.get("customer") or "").strip():
                from apps.crm.models import Customer
                customer = services.by_pk(
                    Customer.objects.filter(company=company), request.POST.get("customer"))
                if customer is None:
                    raise ValidationError(
                        "That customer is not on this company's list — pick one from the "
                        "box or leave it as a counter sale.")
            salesperson = None
            if (request.POST.get("salesperson") or "").strip():
                salesperson = services.by_pk(services.company_people(company),
                                             request.POST.get("salesperson"))
                if salesperson is None:
                    raise ValidationError("That salesperson does not work in this company.")
            with transaction.atomic():
                sale = services.checkout(
                    company, request.user, quantities,
                    session=cashier_session,
                    payment_method=request.POST.get("payment_method") or "CASH",
                    tendered=request.POST.get("amount_tendered"),
                    customer=customer,
                    warehouse=request.POST.get("warehouse") or None,
                    notes=request.POST.get("notes") or "",
                    token=token,
                    discounts=discounts,
                    salesperson=salesperson,
                    customer_reference=request.POST.get("customer_reference") or "")
        except IntegrityError:
            # Two copies of the same form raced each other; the database let
            # one through and refused the other. Hand back the one that won.
            already = services.submitted_ticket(cashier_session, token)
            if already:
                return self.answer_spent_token(request, already, cashier_session, company,
                                               quantities)
            raise
        except ValidationError as exc:
            messages.error(request, f"Could not ring that ticket: {exc.messages[0]}")
            return render(request, self.template_name,
                          self.get_context_data(cashier_session, company, quantities, discounts),
                          status=200)
        messages.success(
            request,
            f"{sale.reference}: {sale.total_amount} taken, {sale.change_given} change given.")
        return redirect("pos:sale_detail", pk=sale.pk)

    def answer_spent_token(self, request, found, cashier_session, company, quantities):
        """The token came back already spent: the same ticket again, or not ours."""
        refusal = services.token_refusal(found)
        if refusal:
            messages.error(request, refusal)
            return render(request, self.template_name,
                          self.get_context_data(cashier_session, company, quantities),
                          status=200)
        return self.already_rung(request, found)

    @staticmethod
    def already_rung(request, sale):
        """The same form came back twice: show the ticket it already made."""
        messages.info(
            request,
            f"{sale.reference} was already rung from this form — it was not taken twice.")
        return redirect("pos:sale_detail", pk=sale.pk)


class SessionDetailView(LoginRequiredMixin, DetailView):
    model = POSSession
    template_name = "pos/session_detail.html"
    context_object_name = "session"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return POSSession.objects.none()
        return POSSession.objects.filter(company=company)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["report"] = services.till_report(self.object)
        ctx["sales"] = self.object.sales.select_related(
            "customer", "warehouse").order_by("-created_at")[:50]
        ctx["can_close"] = services.may_handle(self.object, self.request.user)
        return ctx


@login_required
@require_POST
def session_close(request, pk):
    """Stop trading on this till, with the count of the drawer if there is one.

    Only the till's own cashier — or a company administrator standing in for
    them — may close it; the service refuses anyone else.
    """
    company = request.active_company
    session = get_object_or_404(POSSession, pk=pk, company=company)
    try:
        session = services.close_session(
            session, closing_cash=request.POST.get("closing_cash"), user=request.user)
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect("pos:session_detail", pk=session.pk)
    report = services.till_report(session)
    if report["variance"] is None:
        messages.warning(
            request,
            f"Session closed. Nothing was counted against the expected {report['expected_cash']} "
            "— record the count on the cash-up sheet to prove the drawer.")
    else:
        messages.success(
            request,
            f"Session closed: counted {report['counted_cash']} against expected "
            f"{report['expected_cash']}, variance {report['variance']}"
            + (f" — written off to cash over and short in {session.variance_entry.reference}."
               if session.variance_entry_id else "."))
    if request.POST.get("then") == "cash_up":
        # F9 Cash Out / Close Day lands on the cash-up sheet it just counted.
        return redirect("pos:cash_up", pk=session.pk)
    return redirect("pos:session_detail", pk=session.pk)


class SaleListView(LoginRequiredMixin, ListView):
    model = POSSale
    template_name = "pos/sale_list.html"
    context_object_name = "sales"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return POSSale.objects.none()
        return POSSale.objects.filter(session__company=company).select_related(
            "session", "customer", "warehouse")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Till Tickets"
        return ctx


class SaleDetailView(LoginRequiredMixin, DetailView):
    """The ticket as the customer left with it, and the way to give it back."""
    model = POSSale
    template_name = "pos/sale_detail.html"
    context_object_name = "sale"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return POSSale.objects.none()
        return POSSale.objects.filter(session__company=company).select_related(
            "session", "session__cashier", "customer", "warehouse", "journal_entry")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        sale = self.object
        ctx["page_title"] = f"Ticket {sale.reference}"
        ctx["lines"] = sale.lines.select_related("product", "tax_rate")
        ctx["refunds"] = POSSale.objects.filter(refund_of=sale).select_related("warehouse")
        # Summed, not overwritten: a line refunded in three goes has three
        # refund lines, and what is still returnable is what all of them left.
        refunded = {}
        for line in POSSaleLine.objects.filter(sale__refund_of=sale):
            refunded[line.product_id] = refunded.get(line.product_id, Decimal("0")) + line.quantity
        ctx["refunded_so_far"] = refunded
        ctx["refundable"] = [
            {"line": line,
             "left": line.quantity - refunded.get(line.product_id, Decimal("0"))}
            for line in sale.lines.select_related("product")]
        # A refund is paid from the refunder's own open till, so the form is
        # offered to anyone standing at one — whichever till rang the sale.
        company = self.request.active_company
        refund_till = services.active_session(company, self.request.user)
        ctx["refund_till"] = refund_till
        ctx["can_refund"] = (not sale.refund_of_id
                             and sale.status in ("COMPLETED", "PARTIAL_REFUND")
                             and refund_till is not None)
        ctx["refund_needs_till"] = (not sale.refund_of_id
                                    and sale.status in ("COMPLETED", "PARTIAL_REFUND")
                                    and refund_till is None)
        # Only a supervisor may refund by a method other than the sale's own.
        ctx["refund_methods"] = (
            [(v, label) for v, label in POSSale.PAYMENT_METHODS if v in services.REFUND_METHODS]
            if services.is_supervisor(company, self.request.user) else [])
        ctx["refund_token"] = services.new_token()
        return ctx


@login_required
@require_POST
def sale_refund(request, pk):
    """Take goods back and hand the money over the counter again.

    Booked on the refunder's own open till; the same refund form submitted twice
    hands back the refund it already made.
    """
    company = request.active_company
    sale = get_object_or_404(POSSale, pk=pk, session__company=company)
    quantities = {key[len("refund_qty_"):]: value
                  for key, value in request.POST.items() if key.startswith("refund_qty_")}
    token = services.clean_token(request.POST.get("refund_token"))
    if not token:
        # As at the terminal: without the form's one-time token a repeated post
        # could not be told from a second refund, and would pay out twice.
        messages.error(
            request,
            f"That refund form had no one-time token, so it could not be protected against "
            f"paying out twice. Nothing was refunded — open {sale.reference} and refund it "
            "from its page.")
        return redirect("pos:sale_detail", pk=pk)
    till = services.active_session(company, request.user)

    def spent(found):
        """Answer a token already used on this till: hand back, or refuse."""
        refusal = services.token_refusal(found, refund_of=sale)
        if refusal:
            messages.error(request, refusal)
            return redirect("pos:sale_detail", pk=sale.pk)
        messages.info(
            request,
            f"{found.reference} was already refunded from this form — it was not paid twice.")
        return redirect("pos:sale_detail", pk=found.pk)

    already = services.submitted_ticket(till, token)
    if already:
        return spent(already)
    try:
        with transaction.atomic():
            ticket = services.refund(
                sale, request.user, quantities,
                reason=request.POST.get("reason") or "",
                payment_method=request.POST.get("payment_method") or None,
                token=token)
    except IntegrityError:
        # Raced by the same form; or the till was opened between the lookup
        # above and the refund, so the lookup had no till to search.
        already = services.submitted_ticket(services.active_session(company, request.user),
                                            token)
        if already:
            return spent(already)
        raise
    except ValidationError as exc:
        messages.error(request, f"Could not refund {sale.reference}: {exc.messages[0]}")
        return redirect("pos:sale_detail", pk=pk)
    messages.success(
        request,
        f"{ticket.reference}: {ticket.total_amount} refunded, goods back on "
        f"{ticket.warehouse.name if ticket.warehouse else 'stock'}.")
    return redirect("pos:sale_detail", pk=ticket.pk)


class CashUpView(LoginRequiredMixin, DetailView):
    """Count the drawer against the tickets, by how the money was taken.

    A till closed without a count can have its count recorded here afterwards
    (POST closing_cash) — by its cashier or a company administrator, once — and
    the variance is then booked the same way a counted close books it.
    """
    model = POSSession
    template_name = "pos/cash_up.html"
    context_object_name = "session"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return POSSession.objects.none()
        return POSSession.objects.filter(company=company)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["report"] = services.till_report(self.object)
        ctx["page_title"] = "Cash Up"
        ctx["can_count"] = (self.object.status == "CLOSED"
                            and self.object.closing_cash is None
                            and services.may_handle(self.object, self.request.user))
        return ctx

    def post(self, request, *args, **kwargs):
        session = self.get_object()
        try:
            session = services.record_count(
                session, request.POST.get("closing_cash"), user=request.user)
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
            return redirect("pos:cash_up", pk=session.pk)
        report = services.till_report(session)
        messages.success(
            request,
            f"Count recorded: {report['counted_cash']} against expected "
            f"{report['expected_cash']}, variance {report['variance']}"
            + (f" — written off to cash over and short in {session.variance_entry.reference}."
               if session.variance_entry_id else "."))
        return redirect("pos:cash_up", pk=session.pk)


# ─────────────────────────── the till's helpers ─────────────────────────────

@login_required
@require_GET
def item_lookup(request):
    """Products for the till's item modal, code box and price check (JSON).

    GET q (code or name; exact=1 matches a scanned SKU or barcode exactly),
    category (that category and everything under it), warehouse (one of the
    till's own shelves, for the on-hand figure), page (25 a page), and
    stock=all for the on-hand at every warehouse the company runs (the price
    check). Only this company's active products; read only.
    """
    from apps.inventory.models import StockLevel
    from apps.organizations.models import Warehouse

    company = request.active_company
    if not company:
        return JsonResponse({"error": "No company selected."}, status=400)
    cashier_session = services.active_session(company, request.user)
    shelves = services.till_warehouses(company, cashier_session)
    wanted = (request.GET.get("warehouse") or "").strip()
    shelf = services.by_pk(shelves, wanted) if wanted else None
    if shelf is None:
        shelf = (services.issue_warehouse(company, cashier_session)
                 if cashier_session else shelves.first())
    products = services.search_products(
        company, request.GET.get("q") or "", request.GET.get("category") or None,
        exact=request.GET.get("exact") == "1")
    paginator = Paginator(products, services.LOOKUP_PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page") or 1)
    items = list(page.object_list)
    ids = [p.pk for p in items]
    on_hand = {}
    if shelf is not None and ids:
        on_hand = {row["product_id"]: row["quantity"] for row in StockLevel.objects.filter(
            warehouse=shelf, product_id__in=ids).values("product_id", "quantity")}
    every = {}
    if request.GET.get("stock") == "all" and ids:
        names = {w.pk: w.name for w in Warehouse.objects.filter(company=company, is_active=True)}
        for row in StockLevel.objects.filter(product_id__in=ids, warehouse_id__in=names).values(
                "product_id", "warehouse_id", "quantity").order_by("warehouse__name"):
            every.setdefault(row["product_id"], []).append(
                {"warehouse": names[row["warehouse_id"]], "qty": str(row["quantity"])})
    results = []
    for product in items:
        card = services.product_card(product, on_hand.get(product.pk, Decimal("0")))
        card["tax_label"] = (f"{product.tax_rate.name}" if product.tax_rate_id else "No VAT")
        if request.GET.get("stock") == "all":
            card["stock"] = every.get(product.pk, []) if product.track_inventory else None
        results.append(card)
    return JsonResponse({
        "results": results,
        "page": page.number,
        "num_pages": paginator.num_pages,
        "count": paginator.count,
        "page_size": services.LOOKUP_PAGE_SIZE,
        "warehouse": {"id": str(shelf.pk), "name": shelf.name} if shelf else None,
    })


@login_required
@require_POST
def lock(request):
    """F7 Lock Station: put the open till behind the lock screen."""
    company = request.active_company
    cashier_session = services.active_session(company, request.user)
    if not cashier_session:
        return JsonResponse({"ok": False, "error": "There is no open till to lock."}, status=400)
    request.session[LOCK_KEY] = str(cashier_session.pk)
    return JsonResponse({"ok": True})


@login_required
@require_POST
def unlock(request):
    """Lift the lock screen with the signed-in cashier's own password.

    Checked with Django's password hashers and counted against the same
    five-failures-in-fifteen-minutes lockout as the sign-in page: a locked till
    left on a counter is a password prompt anyone can walk up to.
    """
    from apps.accounts.models import LoginAttempt
    from apps.accounts.views import (LOCKED_OUT_REASON, LOGIN_LOCKOUT_WINDOW,
                                     _get_client_ip, login_locked_out)

    user = request.user
    email = user.email or user.username
    ip = _get_client_ip(request)
    agent = request.META.get("HTTP_USER_AGENT", "")
    if login_locked_out(email, ip):
        LoginAttempt.objects.create(user=user, email=email, ip_address=ip, user_agent=agent,
                                    success=False, failure_reason=LOCKED_OUT_REASON)
        minutes = int(LOGIN_LOCKOUT_WINDOW.total_seconds() // 60)
        return JsonResponse({"ok": False, "locked_out": True, "error": (
            f"Too many wrong passwords. The till stays locked for {minutes} minutes — "
            "wait, or ask an administrator.")}, status=429)
    password = request.POST.get("password") or ""
    if not password or not user.check_password(password):
        LoginAttempt.objects.create(user=None, email=email, ip_address=ip, user_agent=agent,
                                    success=False, failure_reason="Invalid password (till unlock)")
        return JsonResponse({"ok": False, "error": "That is not your password."}, status=403)
    LoginAttempt.objects.create(user=user, email=email, ip_address=ip, user_agent=agent,
                                success=True, failure_reason="")
    request.session.pop(LOCK_KEY, None)
    return JsonResponse({"ok": True})


@login_required
@require_POST
def cash_movement(request, pk):
    """F2 Payout / Pay-in: notes out of or into an open drawer, booked at once."""
    company = request.active_company
    session = get_object_or_404(POSSession, pk=pk, company=company)

    def back():
        if request.POST.get("next") == "session":
            return redirect("pos:session_detail", pk=session.pk)
        return redirect("pos:terminal")

    if is_locked(request, services.active_session(company, request.user)):
        messages.error(request, "This till is locked — unlock it first. Nothing was moved.")
        return redirect("pos:terminal")
    try:
        with transaction.atomic():
            movement = services.record_cash_movement(
                session, request.user, request.POST.get("direction"),
                request.POST.get("amount"), request.POST.get("reason"),
                request.POST.get("account") or None,
                token=request.POST.get("movement_token"))
    except IntegrityError:
        movement = services.submitted_movement(session, request.POST.get("movement_token"))
        if movement is None:
            raise
    except (ValidationError, ValueError) as exc:
        text = exc.messages[0] if hasattr(exc, "messages") else str(exc)
        messages.error(request, f"Nothing was moved: {text}")
        return back()
    messages.success(
        request,
        f"{movement.reference}: {movement.amount} {movement.get_direction_display().lower()} "
        f"({movement.reason}) — the drawer should now hold "
        f"{POSSession.objects.get(pk=session.pk).expected_cash}.")
    return back()


@login_required
@require_GET
def sale_find(request):
    """F3/F6: open a past ticket by its number (typed or scanned) to refund it."""
    company = request.active_company
    text = (request.GET.get("ref") or "").strip().upper()
    tickets = POSSale.objects.filter(session__company=company, refund_of__isnull=True)
    sale = tickets.filter(reference__iexact=text).first() if text else None
    if sale is None and text.isdigit():
        found = list(tickets.filter(reference__endswith=text.zfill(5))[:2])
        sale = found[0] if len(found) == 1 else None
    if sale is None:
        messages.error(request, f"No till ticket numbered “{text}” on this company's book."
                       if text else "Type or scan the ticket number to return.")
        return redirect("pos:terminal")
    return redirect(f"{reverse('pos:sale_detail', args=[sale.pk])}#refund")
