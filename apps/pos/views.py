"""Point of sale views: the terminal, the till session, and the tickets."""
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView, TemplateView, View

from apps.accounting.currency import book_currency
from apps.core.mixins import CompanyRequiredMixin

from . import services
from .models import POSSale, POSSaleLine, POSSession


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
        return qs

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
        if request.POST.get("branch"):
            branch = Branch.objects.filter(
                pk=request.POST.get("branch"), company=company).first()
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

    def get_context_data(self, cashier_session, company, quantities=None):
        from apps.crm.models import Customer
        from apps.inventory.models import Product, StockLevel

        q = (self.request.GET.get("q") or self.request.POST.get("q") or "").strip()
        products = Product.objects.filter(
            company=company, is_active=True).select_related(
            "tax_rate", "unit_of_measure").order_by("name")
        if q:
            from django.db.models import Q
            products = products.filter(
                Q(name__icontains=q) | Q(sku__icontains=q) | Q(barcode__icontains=q))
        shelf = (services.issue_warehouse(company, cashier_session)
                 if cashier_session else None)
        on_hand = {}
        if shelf:
            on_hand = {row["product_id"]: row["quantity"] for row in StockLevel.objects.filter(
                warehouse=shelf).values("product_id", "quantity")}
        rows = []
        for product in products[:200]:
            rows.append({
                "product": product,
                "quantity": (quantities or {}).get(str(product.pk), ""),
                "on_hand": on_hand.get(product.pk, Decimal("0")) if product.track_inventory else None,
            })
        customers = Customer.objects.filter(company=company, is_active=True).order_by("name")
        methods = [(value, label) for value, label in POSSale.PAYMENT_METHODS if value != "SPLIT"]
        report = services.till_report(cashier_session) if cashier_session else None
        return {
            "page_title": "Till",
            "session": cashier_session,
            "company": company,
            "book": book_currency(company),
            "rows": rows,
            "q": q,
            "customers": customers,
            "methods": methods,
            "warehouses": (list(company.warehouses.filter(is_active=True))
                           if company else []),
            "shelf": shelf,
            "report": report,
            "branches": company.branches.all() if company else [],
            "recent_sales": (POSSale.objects.filter(session=cashier_session)
                             .select_related("customer").order_by("-created_at")[:10]
                             if cashier_session else POSSale.objects.none()),
        }

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
        customer = None
        if request.POST.get("customer"):
            from apps.crm.models import Customer
            customer = Customer.objects.filter(
                pk=request.POST.get("customer"), company=company).first()
        try:
            with transaction.atomic():
                sale = services.checkout(
                    company, request.user, quantities,
                    session=cashier_session,
                    payment_method=request.POST.get("payment_method") or "CASH",
                    tendered=request.POST.get("amount_tendered"),
                    customer=customer,
                    warehouse=request.POST.get("warehouse") or None,
                    notes=request.POST.get("notes") or "")
        except ValidationError as exc:
            messages.error(request, f"Could not ring that ticket: {exc.messages[0]}")
            return render(request, self.template_name,
                          self.get_context_data(cashier_session, company, quantities), status=200)
        messages.success(
            request,
            f"{sale.reference}: {sale.total_amount} taken, {sale.change_given} change given.")
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
        return ctx


@login_required
@require_POST
def session_close(request, pk):
    """Stop trading on this till. Counted cash comes from the cash-up screen."""
    company = request.active_company
    session = get_object_or_404(POSSession, pk=pk, company=company)
    try:
        services.close_session(session, closing_cash=request.POST.get("closing_cash"))
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
            f"{report['expected_cash']}, variance {report['variance']}.")
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
        ctx["refunded_so_far"] = {
            line.product_id: line.quantity for line in
            POSSaleLine.objects.filter(sale__refund_of=sale)}
        ctx["refundable"] = [
            {"line": line,
             "left": line.quantity - ctx["refunded_so_far"].get(line.product_id, Decimal("0"))}
            for line in sale.lines.select_related("product")]
        ctx["can_refund"] = (sale.status in ("COMPLETED", "PARTIAL_REFUND")
                             and sale.session.status == "OPEN")
        return ctx


@login_required
@require_POST
def sale_refund(request, pk):
    """Take goods back and hand the money over the counter again."""
    company = request.active_company
    sale = get_object_or_404(POSSale, pk=pk, session__company=company)
    quantities = {key[len("refund_qty_"):]: value
                  for key, value in request.POST.items() if key.startswith("refund_qty_")}
    try:
        with transaction.atomic():
            ticket = services.refund(
                sale, request.user, quantities,
                reason=request.POST.get("reason") or "",
                payment_method=request.POST.get("payment_method") or None)
    except ValidationError as exc:
        messages.error(request, f"Could not refund {sale.reference}: {exc.messages[0]}")
        return redirect("pos:sale_detail", pk=pk)
    messages.success(
        request,
        f"{ticket.reference}: {ticket.total_amount} refunded, goods back on "
        f"{ticket.warehouse.name if ticket.warehouse else 'stock'}.")
    return redirect("pos:sale_detail", pk=ticket.pk)


class CashUpView(LoginRequiredMixin, DetailView):
    """Count the drawer against the tickets, by how the money was taken."""
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
        return ctx
