from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DetailView, ListView, TemplateView

from apps.core.scoping import CompanyScopedMixin, get_scoped_or_404, scope

from .forms import CloseSessionForm, OpenSessionForm
from .models import POSSale, POSSaleLine, POSSession

# Roles that may close a till they didn't open (a supervisor signing off).
TILL_SUPERVISORS = {"ADMIN", "SALES_MANAGER", "FINANCE_MANAGER"}


class ModuleHomeView(LoginRequiredMixin, TemplateView):
    template_name = "pos/home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Point of Sale"
        company = getattr(self.request, "active_company", None)
        if company:
            ctx["open_session"] = POSSession.objects.filter(
                company=company, cashier=self.request.user, status="OPEN"
            ).first()
            ctx["recent_sessions"] = POSSession.objects.filter(company=company)[:10]
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
        return POSSession.objects.filter(company=company).select_related("cashier", "branch")


class SessionOpenView(LoginRequiredMixin, CreateView):
    model = POSSession
    form_class = OpenSessionForm
    template_name = "pos/session_open.html"
    success_url = reverse_lazy("pos:home")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = getattr(self.request, "active_company", None)
        return kwargs

    def form_valid(self, form):
        open_till = POSSession.objects.filter(company=self.request.active_company, cashier=self.request.user,
                                              status="OPEN").first()
        if open_till:
            form.add_error(None, f"You already have a till open since {open_till.opened_at:%d %b %H:%M}. "
                                 f"Close it before opening another.")
            return self.form_invalid(form)
        form.instance.company = self.request.active_company
        form.instance.cashier = self.request.user
        form.instance.opened_at = timezone.now()
        form.instance.status = "OPEN"
        messages.success(self.request, "POS session opened.")
        return super().form_valid(form)


@login_required
def session_close(request, pk):
    session = get_scoped_or_404(POSSession, request, pk=pk)
    if session.status != "OPEN":
        messages.error(request, f"This till was closed on {session.closed_at:%d %b %Y %H:%M} with "
                                f"{session.closing_cash:,.2f} counted. A closed cash-up can't be changed.")
        return redirect("pos:session_detail", pk=session.pk)
    supervisor = getattr(request, "company_role", None) in TILL_SUPERVISORS or request.user.is_superuser
    if session.cashier_id != request.user.pk and not supervisor:
        messages.error(request, f"Only {session.cashier} or a supervisor can close this till.")
        return redirect("pos:session_detail", pk=session.pk)
    if request.method == "POST":
        form = CloseSessionForm(request.POST, instance=session)
        if form.is_valid():
            session = form.save(commit=False)
            cash_sales = sum((x.total_amount for x in session.sales.filter(payment_method="CASH")), Decimal(0))
            session.expected_cash = session.opening_cash + cash_sales
            session.closed_at = timezone.now()
            session.status = "CLOSED"
            session.save()
            variance = session.closing_cash - session.expected_cash
            note = "balanced" if not variance else f"{'over' if variance > 0 else 'short'} by {abs(variance):,.2f}"
            messages.success(request, f"Till closed: expected {session.expected_cash:,.2f}, counted "
                                      f"{session.closing_cash:,.2f} ({note}).")
            return redirect("pos:session_detail", pk=session.pk)
    else:
        form = CloseSessionForm(instance=session)
    return render(request, "pos/session_close.html", {"form": form, "session": session})


class SessionDetailView(CompanyScopedMixin, LoginRequiredMixin, DetailView):
    model = POSSession
    template_name = "pos/session_detail.html"
    context_object_name = "session"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["sales"] = self.object.sales.all()[:50]
        return ctx


class SaleListView(LoginRequiredMixin, ListView):
    model = POSSale
    template_name = "pos/sale_list.html"
    context_object_name = "sales"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return POSSale.objects.none()
        return POSSale.objects.filter(session__company=company).select_related("session", "customer")


class SaleDetailView(CompanyScopedMixin, LoginRequiredMixin, DetailView):
    model = POSSale
    company_lookup = "session__company"
    template_name = "pos/sale_detail.html"
    context_object_name = "sale"


class TerminalView(LoginRequiredMixin, TemplateView):
    """Cashier terminal: pick products and quantities, take payment.

    Each sale issues stock from the branch's store, posts the takings to cash
    or bank with revenue and VAT, and adds to the till's totals.
    """
    template_name = "pos/terminal.html"
    ROWS = 8

    def _session(self):
        company = self.request.active_company
        return POSSession.objects.filter(
            company=company, cashier=self.request.user, status="OPEN"
        ).select_related("branch").first() if company else None

    def get_context_data(self, **kwargs):
        from apps.inventory.models import Product
        ctx = super().get_context_data(**kwargs)
        ctx["session"] = self._session()
        ctx["products"] = scope(Product.objects.filter(is_active=True), self.request.active_company) \
            .select_related("tax_rate").order_by("name")
        posted = kwargs.get("posted") or {}
        # After a refused sale, keep what the cashier rang up.
        ctx["rows"] = [{"i": i, "product": posted.get(f"product_{i}", ""), "qty": posted.get(f"qty_{i}", "")}
                       for i in range(self.ROWS)]
        ctx["payment_methods"] = POSSale.PAYMENT_METHODS[:4]
        ctx["method"] = posted.get("payment_method", "CASH")
        return ctx

    def post(self, request, *args, **kwargs):
        from apps.core import guards
        from apps.inventory.models import Product
        session = self._session()
        if not session:
            messages.error(request, "Open a till before ringing up sales.")
            return redirect("pos:session_open")
        company = request.active_company
        products = {str(p.pk): p for p in scope(Product.objects.filter(is_active=True), company).select_related("tax_rate")}
        cart, errors = [], []
        for i in range(self.ROWS):
            pid, raw = request.POST.get(f"product_{i}") or "", (request.POST.get(f"qty_{i}") or "").strip()
            if not pid and not raw:
                continue
            product = products.get(pid)
            if not product:
                errors.append(f"Line {i + 1}: choose a product.")
                continue
            try:
                cart.append((product, guards.parse_amount(raw or "1", f"{product.name} quantity")))
            except ValidationError as e:
                errors.extend(e.messages)
        method = request.POST.get("payment_method") or "CASH"
        if method not in dict(POSSale.PAYMENT_METHODS):
            errors.append("Choose how the customer paid.")
        if not cart and not errors:
            errors.append("The cart is empty.")
        lines, subtotal, tax = [], Decimal(0), Decimal(0)
        for product, qty in cart:
            gross = (product.selling_price * qty).quantize(Decimal("0.01"))
            rate = product.tax_rate.rate if product.tax_rate else Decimal(0)
            if product.tax_rate and product.tax_rate.is_inclusive:
                line_tax = (gross * rate / (100 + rate)).quantize(Decimal("0.01"))
                line_total = gross
            else:
                line_tax = (gross * rate / 100).quantize(Decimal("0.01"))
                line_total = gross + line_tax
            lines.append((product, qty, line_tax, line_total))
            subtotal += line_total - line_tax
            tax += line_tax
        total = subtotal + tax
        tendered = total
        if method == "CASH" and not errors:
            try:
                tendered = guards.parse_amount(request.POST.get("amount_tendered") or str(total), "Cash tendered")
            except ValidationError as e:
                errors.extend(e.messages)
            else:
                if tendered < total:
                    errors.append(f"Cash tendered {tendered:,.2f} is less than the total {total:,.2f}.")
        if errors:
            for e in errors:
                messages.error(request, e)
            return self.render_to_response(self.get_context_data(posted=request.POST))

        from apps.core.models import SequenceCounter
        from apps.inventory.models import StockLevel, StockMovement
        from apps.organizations.models import Warehouse
        store = Warehouse.objects.filter(company=company, branch=session.branch, is_active=True) \
            .order_by("-is_default", "name").first() \
            or Warehouse.objects.filter(company=company, is_active=True).order_by("-is_default", "name").first()
        try:
            with transaction.atomic():
                sale = POSSale.objects.create(
                    session=session, reference=SequenceCounter.next_number(company, "POS-"),
                    subtotal=subtotal, tax_amount=tax, total_amount=total, payment_method=method,
                    amount_tendered=tendered, change_given=tendered - total,
                )
                for product, qty, line_tax, line_total in lines:
                    POSSaleLine.objects.create(sale=sale, product=product, quantity=qty,
                                               unit_price=product.selling_price, tax_amount=line_tax,
                                               line_total=line_total)
                    if product.track_inventory and product.product_type == "GOODS":
                        if store is None:
                            raise ValidationError("Set up a warehouse for this branch before selling stock items.")
                        level = StockLevel.objects.filter(product=product, warehouse=store).first()
                        StockMovement.objects.create(
                            product=product, warehouse=store, movement_type="DELIVERY", quantity=qty,
                            unit_cost=(level.avg_cost if level and level.avg_cost else product.cost_price),
                            reference=sale.reference, movement_date=timezone.now().date(),
                            performed_by=request.user, notes=f"Till sale {sale.reference}",
                        )
                from apps.accounting.gl_service import post_pos_sale
                post_pos_sale(sale, user=request.user)
                POSSession.objects.filter(pk=session.pk).update(total_sales=session.total_sales + total)
        except (ValidationError, ValueError) as e:
            messages.error(request, " ".join(getattr(e, "messages", [str(e)])))
            return self.render_to_response(self.get_context_data(posted=request.POST))
        change = f" Change {sale.change_given:,.2f}." if sale.change_given else ""
        messages.success(request, f"Sale {sale.reference}: {total:,.2f} by {sale.get_payment_method_display()}.{change}")
        return redirect("pos:terminal")


class CashUpView(CompanyScopedMixin, LoginRequiredMixin, DetailView):
    """Classic Palladium 'POS Cash-Up' screen — reconciles counted cash
    against expected sales for a session."""
    model = POSSession
    template_name = "pos/cash_up.html"
    context_object_name = "session"

    def get_context_data(self, **kwargs):
        from decimal import Decimal
        ctx = super().get_context_data(**kwargs)
        s = self.object
        cash_sales = sum(
            (x.total_amount for x in s.sales.filter(payment_method="CASH")),
            Decimal(0),
        )
        card_sales = sum((x.total_amount for x in s.sales.filter(payment_method="CARD")), Decimal(0))
        mpesa_sales = sum((x.total_amount for x in s.sales.filter(payment_method="MPESA")), Decimal(0))
        bank_sales = sum((x.total_amount for x in s.sales.filter(payment_method="BANK")), Decimal(0))
        expected = s.opening_cash + cash_sales
        counted = s.closing_cash or Decimal(0)
        ctx.update({
            "cash_sales": cash_sales, "card_sales": card_sales,
            "mpesa_sales": mpesa_sales, "bank_sales": bank_sales,
            "expected_cash": expected, "counted_cash": counted,
            "variance": counted - expected,
        })
        return ctx
