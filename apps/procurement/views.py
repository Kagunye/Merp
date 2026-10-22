"""Procurement views."""
import datetime
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, TemplateView, UpdateView

from apps.core.mixins import CompanyRequiredMixin

from .forms import BillForm
from .models import (
    Bill, PurchaseOrder, PurchaseOrderLine, PurchaseRequisition,
)
from apps.core.forms import ISO_DATE


def _post_bill_to_gl(request, bill, user=None) -> bool:
    """Post a bill to the ledger, surfacing failures instead of skipping silently."""
    try:
        from apps.accounting.gl_service import post_bill
        post_bill(bill, user=user)
        return True
    except Exception as exc:
        messages.error(
            request,
            f"Bill '{bill.reference}' saved, but GL posting failed: {exc}",
        )
        return False


class BillListView(LoginRequiredMixin, ListView):
    template_name = "procurement/bill_list.html"
    context_object_name = "bills"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Bill.objects.none()
        qs = Bill.objects.filter(company=company, is_active=True).select_related("supplier")
        status = self.request.GET.get("status")
        if status:
            qs = qs.filter(status=status)
        q = self.request.GET.get("q")
        if q:
            qs = qs.filter(Q(reference__icontains=q) | Q(supplier__name__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Supplier Bills"
        ctx["status_choices"] = Bill.STATUS_CHOICES
        ctx["status_filter"] = self.request.GET.get("status", "")
        ctx["q"] = self.request.GET.get("q", "")
        return ctx


class BillCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    model = Bill
    form_class = BillForm
    template_name = "procurement/bill_form.html"

    def get_success_url(self):
        from django.urls import reverse
        return reverse("procurement:bill_detail", kwargs={"pk": self.object.pk})

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def form_valid(self, form):
        company = self.request.active_company
        bill = form.save(commit=False)
        bill.company = company
        if not bill.reference:
            from apps.core.models import SequenceCounter
            bill.reference = SequenceCounter.next_number(company, "BILL-")
        bill.save()
        self.object = bill
        # The bill must survive a posting failure (it stays DRAFT for re-posting),
        # so this runs outside the caller's transaction.
        posted = _post_bill_to_gl(self.request, bill, self.request.user)
        if posted:
            messages.success(self.request, f"Bill '{bill.reference}' created and posted to the GL.")
        else:
            messages.info(
                self.request,
                f"Bill '{bill.reference}' is still DRAFT. Use Post to GL once the issue above is fixed.",
            )
        return redirect(self.get_success_url())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "New Supplier Bill"
        return ctx


class BillDetailView(LoginRequiredMixin, DetailView):
    model = Bill
    template_name = "procurement/bill_detail.html"
    context_object_name = "bill"

    def get_queryset(self):
        return Bill.objects.filter(company=self.request.active_company)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Bill: {self.object.reference}"
        from apps.accounting.models import Account
        ctx["cash_accounts"] = Account.objects.filter(
            company=self.request.active_company, account_type__in=["BANK", "CASH"],
            is_active=True,
        ).order_by("code")
        ctx["today"] = datetime.date.today()
        return ctx


class PurchaseOrderListView(LoginRequiredMixin, ListView):
    template_name = "procurement/po_list.html"
    context_object_name = "orders"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return PurchaseOrder.objects.none()
        qs = PurchaseOrder.objects.filter(company=company, is_active=True).select_related("supplier")
        status = self.request.GET.get("status")
        if status:
            qs = qs.filter(status=status)
        q = self.request.GET.get("q")
        if q:
            qs = qs.filter(Q(reference__icontains=q) | Q(supplier__name__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Purchase Orders"
        ctx["status_choices"] = PurchaseOrder.STATUS_CHOICES
        ctx["status_filter"] = self.request.GET.get("status", "")
        ctx["q"] = self.request.GET.get("q", "")
        return ctx


class PurchaseOrderCreateView(LoginRequiredMixin, CreateView):
    model = PurchaseOrder
    template_name = "procurement/po_form.html"
    fields = ["reference", "supplier", "order_date", "expected_delivery_date", "warehouse", "notes"]

    def get_success_url(self):
        from django.urls import reverse
        return reverse("procurement:po_detail", kwargs={"pk": self.object.pk})

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        company = self.request.active_company
        from django import forms as dforms
        for fname, field in form.fields.items():
            if isinstance(field.widget, dforms.Select):
                field.widget.attrs["class"] = "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
            elif isinstance(field.widget, dforms.Textarea):
                field.widget.attrs["class"] = "form-textarea w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
                field.widget.attrs["rows"] = 3
            elif isinstance(field.widget, dforms.DateInput):
                field.widget.attrs["class"] = "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
                field.widget.attrs["type"] = "date"
                field.widget.format = ISO_DATE
            else:
                field.widget.attrs["class"] = "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
        if company:
            from apps.crm.models import Supplier
            from apps.organizations.models import Warehouse
            form.fields["supplier"].queryset = Supplier.objects.filter(company=company, is_active=True)
            form.fields["warehouse"].queryset = Warehouse.objects.filter(company=company, is_active=True)
            from apps.core.models import SequenceCounter
            form.fields["reference"].initial = SequenceCounter.next_number(company, "PO-")
        form.fields["order_date"].initial = datetime.date.today()
        return form

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        messages.success(self.request, f"Purchase Order '{form.instance.reference}' created.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "New Purchase Order"
        return ctx


class PurchaseOrderDetailView(LoginRequiredMixin, DetailView):
    model = PurchaseOrder
    template_name = "procurement/po_detail.html"
    context_object_name = "po"

    def get_queryset(self):
        return PurchaseOrder.objects.filter(company=self.request.active_company)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"PO: {self.object.reference}"
        ctx["lines"] = self.object.lines.select_related("product")
        ctx["bills"] = self.object.bills.all()
        # The add-line form on this page needs the same company-scoped choices
        # a form class would have given it.
        from apps.accounting.models import TaxRate
        from apps.inventory.models import Product
        ctx["products"] = Product.objects.filter(
            company=self.object.company, is_active=True).order_by("name")
        ctx["tax_rates"] = TaxRate.objects.filter(
            company=self.object.company, is_active=True).order_by("name")
        ctx["warehouses"] = self.object.company.warehouses.filter(is_active=True).order_by("name")
        return ctx


class SupplierStatementView(LoginRequiredMixin, TemplateView):
    """Full AP statement for a supplier: bills, debit notes, what is settled.

    A supplier can be billed in more than one money, so the statement is read
    in the book's currency: each row shows the paper in its own figures and the
    value it entered the ledger at, and the running balance is the money owed.
    """
    template_name = "procurement/supplier_statement.html"

    # What the ledger has seen — a draft bill is a document still being
    # checked, and a cancelled one is offset by its own reversal.
    ON_ACCOUNT = ["RECEIVED", "PARTIAL", "PAID", "OVERDUE", "DEBITED", "DEBIT_NOTE"]

    def get_context_data(self, **kwargs):
        from decimal import Decimal
        from apps.accounting.currency import book_currency, book_value
        from apps.accounting.models import to_base
        from apps.crm.models import Supplier
        from .models import Bill
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        supplier = get_object_or_404(Supplier, pk=kwargs["pk"], company=company)
        book = book_currency(company)
        start = self.request.GET.get("from") or ""
        end = self.request.GET.get("to") or ""
        currency = (self.request.GET.get("currency") or "").upper()
        try:
            from_date = datetime.date.fromisoformat(start) if start else None
            to_date = datetime.date.fromisoformat(end) if end else None
        except ValueError:
            from_date = to_date = None

        def bills(statuses=None):
            qs = Bill.objects.filter(
                supplier=supplier, status__in=(statuses or self.ON_ACCOUNT))
            return qs.filter(currency=currency) if currency else qs

        def bill_row(bill):
            base = book_value(bill)
            noted = (bill.status == "DEBIT_NOTE"
                     or bill.original_bill_id is not None)
            # Money that left the account is in the bill's own currency, so it
            # is read back at the rate the bill was raised at.
            paid = to_base(bill.paid_amount, bill.rate)
            return {
                "date": bill.bill_date,
                "type": "Debit note" if noted else "Bill",
                "reference": bill.reference,
                "detail": bill.get_status_display(),
                "due_date": bill.due_date,
                "currency": bill.currency,
                "paper": bill.total_amount,
                "rate": bill.exchange_rate,
                # What we owe goes up with a bill and down with a debit note —
                # the note is shown gross, and the bill it answers is left at
                # its own full value, exactly as the ledger holds the pair.
                "owed": Decimal("0") if noted else base,
                "debited": base if noted else Decimal("0"),
                "paid": paid,
                "outstanding": bill.outstanding_base,
                "url": reverse("procurement:bill_detail", args=[bill.pk]),
            }

        def movement(row):
            return row["owed"] - row["debited"] - row["paid"]

        window_qs = bills()
        if from_date:
            window_qs = window_qs.filter(bill_date__gte=from_date)
        if to_date:
            window_qs = window_qs.filter(bill_date__lte=to_date)
        window = sorted((bill_row(b) for b in window_qs),
                        key=lambda r: (r["date"], str(r["reference"])))

        opening = sum((movement(bill_row(b))
                       for b in bills().filter(bill_date__lt=from_date)),
                      Decimal("0")) if from_date else Decimal("0")

        running = opening
        for r in window:
            running += movement(r)
            r["balance"] = running

        still_owed = sum(
            (b.outstanding_base for b in bills(["RECEIVED", "PARTIAL", "OVERDUE"])
             if b.outstanding_amount > 0), Decimal("0"))

        seen = {r["currency"] for r in window}
        ctx["supplier"] = supplier
        ctx["book"] = book
        ctx["rows"] = window
        ctx["total_billed"] = sum((r["owed"] for r in window), Decimal("0"))
        ctx["total_paid"] = sum((r["paid"] for r in window), Decimal("0"))
        ctx["total_credited"] = sum((r["debited"] for r in window), Decimal("0"))
        ctx["opening_balance"] = opening
        ctx["closing_balance"] = running
        ctx["still_owed"] = still_owed
        ctx["currencies"] = sorted(
            seen | set(Bill.objects.filter(supplier=supplier)
                       .values_list("currency", flat=True)))
        ctx["mixed"] = len(seen) > 1
        ctx["currency"] = currency
        ctx["from_date"] = start
        ctx["to_date"] = end
        ctx["start_on"] = from_date
        ctx["end_on"] = to_date
        return ctx


class APAgingView(LoginRequiredMixin, TemplateView):
    """AP aging buckets across all suppliers."""
    template_name = "procurement/ap_aging.html"

    def get_context_data(self, **kwargs):
        from datetime import date
        from decimal import Decimal
        from apps.crm.models import Supplier
        from .models import Bill
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if not company:
            return ctx
        today = date.today()
        buckets = {"current": Decimal(0), "d30": Decimal(0), "d60": Decimal(0), "d90": Decimal(0), "d120": Decimal(0)}
        rows = []
        for sup in Supplier.objects.filter(company=company):
            per = {"supplier": sup, "current": Decimal(0), "d30": Decimal(0), "d60": Decimal(0), "d90": Decimal(0), "d120": Decimal(0), "total": Decimal(0)}
            for b in Bill.objects.filter(supplier=sup, status__in=["RECEIVED", "PARTIAL"]):
                # Owed in our money: dollars and shillings only add up once each
                # has been converted at the rate its bill was raised at.
                out = b.outstanding_base
                age = (today - b.due_date).days if b.due_date else 0
                if age <= 0: key = "current"
                elif age <= 30: key = "d30"
                elif age <= 60: key = "d60"
                elif age <= 90: key = "d90"
                else: key = "d120"
                per[key] += out; per["total"] += out
                buckets[key] += out
            if per["total"] > 0:
                rows.append(per)
        ctx["rows"] = rows
        ctx["buckets"] = buckets
        ctx["book"] = (company.currency or "").upper()
        return ctx


class ProcurementIndexView(LoginRequiredMixin, TemplateView):
    """AP module home — KPI dashboard summarising payables."""
    template_name = "procurement/index.html"

    def get_context_data(self, **kwargs):
        from datetime import date
        from decimal import Decimal
        from .models import Bill, PurchaseOrder, PurchaseRequisition
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if not company:
            return ctx
        bills = Bill.objects.filter(company=company)
        today = date.today()
        from apps.accounting.currency import book_currency
        ctx["book"] = book_currency(company)
        # The KPIs are the ledger's money, not the sum of several papers.
        open_bills = [b for b in bills.filter(status__in=["RECEIVED", "PARTIAL", "OVERDUE"])
                      if b.outstanding_amount > 0]
        ctx["total_outstanding"] = sum(
            (b.outstanding_base for b in open_bills), Decimal(0))
        ctx["total_overdue"] = sum(
            (b.outstanding_base for b in open_bills if b.due_date < today),
            Decimal(0))
        ctx["open_po_count"] = PurchaseOrder.objects.filter(company=company, status__in=["DRAFT", "SENT", "PARTIALLY_RECEIVED"]).count()
        ctx["pending_req_count"] = PurchaseRequisition.objects.filter(company=company, status="PENDING_APPROVAL").count()
        ctx["recent_bills"] = bills.order_by("-bill_date")[:8]
        return ctx


class PurchaseRequisitionListView(LoginRequiredMixin, ListView):
    model = PurchaseRequisition
    template_name = "procurement/requisition_list.html"
    context_object_name = "requisitions"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return PurchaseRequisition.objects.none()
        return PurchaseRequisition.objects.filter(company=company).select_related("requested_by")


class PurchaseRequisitionCreateView(LoginRequiredMixin, CreateView):
    model = PurchaseRequisition
    template_name = "procurement/requisition_form.html"

    def get_form_class(self):
        from .forms import PurchaseRequisitionForm
        return PurchaseRequisitionForm

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        form.instance.requested_by = self.request.user
        messages.success(self.request, "Purchase requisition created.")
        return super().form_valid(form)

    def get_success_url(self):
        from django.urls import reverse
        return reverse("procurement:requisition_detail", args=[self.object.pk])


class PurchaseRequisitionDetailView(LoginRequiredMixin, DetailView):
    model = PurchaseRequisition
    template_name = "procurement/requisition_detail.html"
    context_object_name = "requisition"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return PurchaseRequisition.objects.none()
        return PurchaseRequisition.objects.filter(company=company)


class GRNView(LoginRequiredMixin, DetailView):
    """Goods Receipt Note — view & record what came in against a PO."""
    model = PurchaseOrder
    template_name = "procurement/grn.html"
    context_object_name = "po"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return PurchaseOrder.objects.none()
        return PurchaseOrder.objects.filter(company=company)

    def get_context_data(self, **kwargs):
        from apps.accounting.currency import book_currency
        ctx = super().get_context_data(**kwargs)
        ctx["warehouses"] = self.object.company.warehouses.filter(is_active=True).order_by("name")
        ctx["book"] = book_currency(self.object.company)
        return ctx


# ------------------------- PO lines, goods receipt -------------------------


def _decimal(raw, default=None):
    """A posted number, or None when it is blank, nonsense or negative."""
    if raw in (None, ""):
        return default
    try:
        value = Decimal(str(raw))
    except Exception:
        return None
    return value if value >= 0 else None


@login_required
@require_POST
def po_line_add(request, pk):
    """Add one line to a purchase order.

    The header totals are whatever the lines say they are, so they are not
    typed in beside them — a bill must be able to trust the order it came from.
    """
    from apps.core.models import SequenceCounter
    from apps.inventory.models import Product

    po = get_object_or_404(PurchaseOrder, pk=pk, company=request.active_company)
    if po.status in ("BILLED", "CANCELLED"):
        messages.error(request, f"PO {po.reference} is {po.get_status_display()}, cannot add lines.")
        return redirect("procurement:po_detail", pk=pk)

    raw_product = request.POST.get("product") or ""
    try:
        product = Product.objects.filter(pk=raw_product, company=po.company, is_active=True).first()
    except ValueError:
        product = None
    if not product:
        messages.error(request, "Choose a product from this company.")
        return redirect("procurement:po_detail", pk=pk)

    quantity = _decimal(request.POST.get("quantity"))
    if quantity is None or quantity <= 0:
        messages.error(request, "Quantity must be a number greater than zero.")
        return redirect("procurement:po_detail", pk=pk)
    unit_cost = _decimal(request.POST.get("unit_cost"))
    if unit_cost is None:
        messages.error(request, "Unit cost must be a number of zero or more.")
        return redirect("procurement:po_detail", pk=pk)

    from apps.accounting.models import TaxRate
    raw_tax = request.POST.get("tax_rate") or ""
    tax_rate = None
    if raw_tax:
        tax_rate = TaxRate.objects.filter(pk=raw_tax, company=po.company).first()
        if not tax_rate:
            messages.error(request, "That tax rate does not belong to this company.")
            return redirect("procurement:po_detail", pk=pk)

    with transaction.atomic():
        PurchaseOrderLine.objects.create(
            order=po, product=product,
            description=(request.POST.get("description") or "").strip() or product.name,
            quantity=quantity, unit_cost=unit_cost, tax_rate=tax_rate,
        )
        po.calculate_totals()
    messages.success(request, f"{product.name} added to {po.reference} — order total {po.total_amount}.")
    return redirect("procurement:po_detail", pk=pk)


@login_required
@require_POST
def po_line_delete(request, pk, line_pk):
    po = get_object_or_404(PurchaseOrder, pk=pk, company=request.active_company)
    line = get_object_or_404(PurchaseOrderLine, pk=line_pk, order=po)
    if line.received_quantity > 0 or line.billed_quantity > 0:
        messages.error(
            request,
            f"Cannot remove {line.product.name}: goods have been received or billed "
            "against this line. Return them, or credit the bill, instead.")
    else:
        line.delete()
        po.calculate_totals()
        messages.success(request, f"{line.product.name} removed from {po.reference}.")
    return redirect("procurement:po_detail", pk=pk)


@login_required
@require_POST
def grn_record(request, pk):
    """Book goods in against a purchase order.

    A receipt changes both quantities and values: the goods are on the shelf
    from the day they arrive, at the cost the order was placed at, converted
    into the book's money at the order's own rate. Because no bill has been
    received for them yet, the value is credited to Stock Received Not
    Invoiced, and the supplier's bill later releases that accrual instead of
    charging the same goods into stock a second time.
    """
    from apps.accounting.currency import to_base
    from apps.inventory.models import StockMovement
    from apps.organizations.models import Warehouse

    po = get_object_or_404(
        PurchaseOrder.objects.filter(company=request.active_company).select_related("warehouse"), pk=pk)
    if po.status not in ("SENT", "PARTIALLY_RECEIVED", "FULLY_RECEIVED"):
        messages.error(
            request,
            f"PO {po.reference} is {po.get_status_display()}: send it to the supplier "
            "before receiving anything against it.")
        return redirect("procurement:po_detail", pk=pk)

    warehouse = po.warehouse
    raw_wh = request.POST.get("warehouse") or ""
    if raw_wh:
        warehouse = Warehouse.objects.filter(pk=raw_wh, company=po.company).first()
        if not warehouse:
            messages.error(request, "That warehouse does not belong to this company.")
            return redirect("procurement:grn", pk=pk)
    if not warehouse:
        warehouse = (Warehouse.objects.filter(company=po.company, is_default=True).first()
                     or Warehouse.objects.filter(company=po.company).first())
    if not warehouse:
        messages.error(request, "This company has no warehouse to receive goods into.")
        return redirect("procurement:grn", pk=pk)

    wanted = []
    for line in po.lines.select_related("product"):
        raw = request.POST.get(f"receive_qty_{line.pk}")
        if raw in (None, ""):
            continue  # an empty box means nothing arrived for that line
        try:
            qty = Decimal(str(raw))
        except Exception:
            messages.error(request, f"'{raw}' is not a quantity.")
            return redirect("procurement:grn", pk=pk)
        if qty < 0:
            messages.error(request, "Received quantities cannot be negative.")
            return redirect("procurement:grn", pk=pk)
        if qty > line.to_receive:
            messages.error(
                request,
                f"Cannot receive {qty} of {line.product.name}: only {line.to_receive} "
                "of this line is still outstanding.")
            return redirect("procurement:grn", pk=pk)
        if qty > 0:
            wanted.append((line, qty))
    if not wanted:
        messages.error(request, "Enter at least one quantity to receive.")
        return redirect("procurement:grn", pk=pk)

    today = datetime.date.today()
    rate = po.rate
    try:
        with transaction.atomic():
            for line, qty in wanted:
                if line.product.track_inventory:
                    StockMovement.objects.create(
                        product=line.product, warehouse=warehouse, movement_type="RECEIPT",
                        quantity=qty, unit_cost=to_base(line.unit_cost, rate),
                        purchase_order=po,
                        movement_date=today, reference=po.reference,
                        performed_by=request.user,
                        notes=f"Goods received against {po.reference}",
                    )
                line.received_quantity = (line.received_quantity or Decimal("0")) + qty
                line.save(update_fields=["received_quantity"])
            received_all = all(l.received_quantity >= l.quantity for l in po.lines.all())
            po.status = "FULLY_RECEIVED" if received_all else "PARTIALLY_RECEIVED"
            po.save(update_fields=["status", "updated_at"])
    except ValidationError as exc:
        messages.error(request, f"Could not receive against {po.reference}: {exc.messages[0]}")
        return redirect("procurement:grn", pk=pk)

    messages.success(
        request,
        f"Received {len(wanted)} line(s) into {warehouse.name}. "
        f"{po.reference} is now {po.get_status_display()}.")
    return redirect("procurement:po_detail", pk=pk)


# ------------------------- conversion: order to bill -------------------------


@login_required
@require_POST
def po_to_bill(request, pk):
    """Raise a supplier bill for what has been received and not yet billed.

    The bill is left as a draft: the supplier's own invoice is a document that
    arrives from outside, and the person who enters it should see the order,
    the receipt and the amount they are about to owe all on one screen first.
    """
    from apps.accounting.currency import book_currency, stamp_document
    from apps.core.models import SequenceCounter
    from django.core.exceptions import ValidationError

    po = get_object_or_404(
        PurchaseOrder.objects.filter(company=request.active_company).select_related("supplier"), pk=pk)
    if po.status not in ("SENT", "PARTIALLY_RECEIVED", "FULLY_RECEIVED"):
        messages.error(
            request,
            f"PO {po.reference} is {po.get_status_display()}: it cannot be billed "
            "before it is sent and goods have arrived.")
        return redirect("procurement:po_detail", pk=pk)

    outstanding = [(l, l.to_bill) for l in po.lines.all()]
    outstanding = [(l, q) for l, q in outstanding if q > 0]
    if not outstanding:
        received = sum((l.received_quantity for l in po.lines.all()), Decimal("0"))
        messages.error(
            request,
            f"Nothing left to bill on {po.reference}. "
            + ("Record the goods receipt first — the bill is raised against what arrived."
               if received == 0 else "Everything received has already been billed."))
        return redirect("procurement:po_detail", pk=pk)

    subtotal = sum((l.unit_cost * q for l, q in outstanding), Decimal("0"))
    tax = sum((l.unit_cost * q * ((l.tax_rate.rate if l.tax_rate else Decimal("0")) / 100)
               for l, q in outstanding), Decimal("0"))

    today = datetime.date.today()
    with transaction.atomic():
        bill = Bill(
            company=po.company, supplier=po.supplier, purchase_order=po,
            reference=SequenceCounter.next_number(po.company, "BILL-"),
            bill_date=today, due_date=today + datetime.timedelta(days=30),
            currency=po.currency, exchange_rate=po.exchange_rate,
            subtotal=subtotal, tax_amount=tax, total_amount=subtotal + tax,
            status="DRAFT", notes=f"Raised from PO {po.reference}",
        )
        if bill.currency != book_currency(po.company) and not bill.exchange_rate:
            try:
                stamp_document(bill, today)
            except ValidationError as exc:
                messages.error(request, exc.messages[0])
                return redirect("procurement:po_detail", pk=pk)
        bill.save()
        for line, qty in outstanding:
            line.billed_quantity = (line.billed_quantity or Decimal("0")) + qty
            line.save(update_fields=["billed_quantity"])
        # Only an order that has both arrived and been billed is finished: a
        # line nobody has received yet has nothing to bill, and must not let
        # the order close itself before the rest of the goods turn up.
        settled = all(l.received_quantity >= l.quantity
                      and l.billed_quantity >= l.quantity for l in po.lines.all())
        if settled:
            po.status = "BILLED"
            po.save(update_fields=["status", "updated_at"])

    messages.success(
        request,
        f"Bill {bill.reference} for {bill.total_amount} {bill.currency} raised from "
        f"{po.reference}. It is a draft — check it against the supplier's invoice, then post it.")
    return redirect("procurement:bill_detail", pk=bill.pk)


@login_required
@require_POST
def bill_debit_note(request, pk):
    """Return goods or accept a price reduction, and put it in the book.

    The bill keeps the entry it was posted with. The debit note is a second
    document with its own mirrored entry, and the two together say what is
    actually owed — which is more than an erased line ever told anybody.
    """
    from apps.core.models import SequenceCounter

    bill = get_object_or_404(
        Bill.objects.filter(company=request.active_company).select_related("supplier"), pk=pk)
    if not bill.journal_entry_id:
        messages.error(
            request,
            f"{bill.reference} has not been posted to the GL, so there is nothing to "
            "debit. Edit or cancel the draft instead.")
        return redirect("procurement:bill_detail", pk=pk)
    if bill.status in ("CANCELLED", "DEBIT_NOTE"):
        messages.error(request, f"Cannot debit a {bill.get_status_display().lower()}.")
        return redirect("procurement:bill_detail", pk=pk)

    amount = _decimal(request.POST.get("amount"))
    if amount is None or amount <= 0:
        messages.error(request, "Enter the amount to debit, in the bill's own currency.")
        return redirect("procurement:bill_detail", pk=pk)
    if amount > bill.outstanding_amount:
        messages.error(
            request,
            f"Debit ({amount} {bill.currency}) is more than {bill.reference} still owes "
            f"({bill.outstanding_amount} {bill.currency}). Pay the supplier a refund instead.")
        return redirect("procurement:bill_detail", pk=pk)

    # A header-level document has no lines to re-price, so the tax is taken at
    # the same proportion the bill charged — the VAT input credited then matches
    # the VAT input debited, at whatever rate was in force on the bill.
    tax = (amount * bill.tax_amount / bill.total_amount).quantize(Decimal("0.01")) \
        if bill.total_amount else Decimal("0")
    tax = min(tax, amount)
    today = datetime.date.today()

    with transaction.atomic():
        note = Bill(
            company=bill.company, supplier=bill.supplier,
            purchase_order_id=bill.purchase_order_id, original_bill=bill,
            reference=SequenceCounter.next_number(bill.company, "DN-"),
            bill_date=today, due_date=today,
            currency=bill.currency, exchange_rate=bill.exchange_rate,
            subtotal=amount - tax, tax_amount=tax, total_amount=amount,
            status="DEBIT_NOTE",
            notes=request.POST.get("reason", "").strip() or f"Debit note against {bill.reference}",
        )
        note.credited_amount = amount
        note.save()
        try:
            from apps.accounting.gl_service import post_debit_note
            post_debit_note(note, user=request.user)
        except Exception as exc:
            transaction.set_rollback(True)
            messages.error(request, f"Debit note could not be posted: {exc}")
            return redirect("procurement:bill_detail", pk=pk)

        bill.credited_amount = (bill.credited_amount or Decimal("0")) + amount
        bill.save()

    messages.success(
        request,
        f"Debit note {note.reference} for {amount} {bill.currency} posted against "
        f"{bill.reference}.")
    return redirect("procurement:bill_detail", pk=bill.pk)


# ------------------------- AP actions -------------------------

class BillUpdateView(LoginRequiredMixin, UpdateView):
    """Edit a bill. Blocks edits once fully paid or cancelled."""
    model = Bill
    form_class = BillForm
    template_name = "procurement/bill_form.html"

    def get_queryset(self):
        return Bill.objects.filter(company=self.request.active_company)

    def dispatch(self, request, *args, **kwargs):
        obj = self.get_object()
        if obj.status in ("PAID", "CANCELLED"):
            messages.error(request, f"Bill {obj.reference} cannot be edited ({obj.get_status_display()}).")
            return redirect("procurement:bill_detail", pk=obj.pk)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_success_url(self):
        from django.urls import reverse
        return reverse("procurement:bill_detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        previous_total = self.object.total_amount
        was_posted = bool(self.object.journal_entry_id)
        response = super().form_valid(form)
        messages.success(self.request, f"Bill {form.instance.reference} updated.")
        if was_posted and self.object.total_amount != previous_total:
            messages.warning(
                self.request,
                f"This bill is already posted to the GL for {previous_total}, but the saved "
                f"bill total is {self.object.total_amount}. Reverse that entry or post a "
                "correction before the ledger and the bill agree again.",
            )
        return response

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit Bill {self.object.reference}"
        ctx["is_edit"] = True
        return ctx


@login_required
@require_POST
def bill_cancel(request, pk):
    """Withdraw a bill.

    An unposted draft never reached the ledger, so calling it off is enough. A
    posted one is answered by an offsetting entry dated in the same period, and
    the order quantities it was drawn from are handed back so the supplier's
    corrected invoice can be entered afresh.
    """
    bill = get_object_or_404(Bill, pk=pk, company=request.active_company)
    if bill.status == "CANCELLED":
        messages.info(request, f"Bill {bill.reference} is already cancelled.")
        return redirect("procurement:bill_detail", pk=pk)
    if bill.paid_amount > 0 or bill.credited_amount > 0:
        messages.error(
            request,
            f"Cannot cancel {bill.reference}: {bill.paid_amount} paid and "
            f"{bill.credited_amount} debited against it. Reverse the payment or "
            "raise a debit note for the part that is wrong.",
        )
        return redirect("procurement:bill_detail", pk=pk)

    with transaction.atomic():
        if bill.journal_entry_id:
            try:
                bill.journal_entry.reverse(user=request.user)
            except Exception as exc:
                messages.error(request, f"Cancel failed: {exc}")
                return redirect("procurement:bill_detail", pk=pk)
        bill.status = "CANCELLED"
        bill.save(update_fields=["status", "updated_at"])
        _release_billed_quantity(request, bill)

    messages.success(
        request,
        f"Bill {bill.reference} cancelled."
        + (" Offsetting entry posted." if bill.journal_entry_id else ""),
    )
    return redirect("procurement:bill_detail", pk=pk)


def _release_billed_quantity(request, bill):
    """Give the order back the quantities a cancelled bill had taken.

    A bill is a header-level document — it records one total, not one figure per
    ordered line — so its quantities can only be attributed back to the order
    when it is the sole bill standing against that order. With two of them live,
    which line each paid for is a guess, and a guessed quantity on an order is
    how a third bill gets raised for goods already paid for. The ledger is
    corrected either way; only the re-billing shortcut is withheld.
    """
    po = bill.purchase_order
    if not po:
        return
    if po.bills.exclude(pk=bill.pk).exclude(status="CANCELLED").exists():
        messages.info(
            request,
            f"{po.reference} still has another bill against it, so its billed "
            "quantities were left as they are rather than guessed at.",
        )
        return
    for line in po.lines.all():
        line.billed_quantity = Decimal("0")
        line.save(update_fields=["billed_quantity"])
    if po.status == "BILLED":
        # The order is no longer closed: say what the quantities actually are,
        # rather than assuming the missing bill was for goods already arrived.
        arrived = all(l.received_quantity >= l.quantity for l in po.lines.all())
        po.status = "FULLY_RECEIVED" if arrived else "PARTIALLY_RECEIVED"
        po.save(update_fields=["status", "updated_at"])


@login_required
@require_POST
def bill_post(request, pk):
    """Post a DRAFT bill's journal entry (Dr Inventory/VAT Input, Cr AP)."""
    bill = get_object_or_404(Bill, pk=pk, company=request.active_company)
    if bill.journal_entry_id:
        messages.info(request, "Bill is already posted to GL.")
    elif bill.status == "CANCELLED":
        messages.error(request, f"Cannot post {bill.reference}: the bill is cancelled.")
    else:
        if _post_bill_to_gl(request, bill, request.user):
            messages.success(request, f"Bill '{bill.reference}' posted to GL.")
    return redirect("procurement:bill_detail", pk=pk)


@login_required
@require_POST
def bill_pay(request, pk):
    """Record a supplier payment against a bill.

    Creates a posted JournalEntry (Dr AP, Cr the chosen Bank/Cash account) and
    updates bill.paid_amount + status. A bill that has not reached the GL is
    posted first, so the debit to payables always has its matching credit.
    """
    from decimal import Decimal
    from apps.accounting.models import Account, JournalEntry, JournalEntryLine
    from apps.core.models import SequenceCounter

    bill = get_object_or_404(Bill, pk=pk, company=request.active_company)
    try:
        amount = Decimal(request.POST.get("amount") or "0")
    except Exception:
        messages.error(request, "Invalid amount.")
        return redirect("procurement:bill_detail", pk=pk)
    if amount <= 0:
        messages.error(request, "Payment amount must be positive.")
        return redirect("procurement:bill_detail", pk=pk)
    if amount > bill.outstanding_amount:
        messages.error(request, f"Payment ({amount}) exceeds outstanding ({bill.outstanding_amount}).")
        return redirect("procurement:bill_detail", pk=pk)

    company = request.active_company
    if not bill.journal_entry_id:
        # Paying an unposted bill would debit AP with no matching credit.
        with transaction.atomic():
            if not _post_bill_to_gl(request, bill, request.user):
                return redirect("procurement:bill_detail", pk=pk)
        messages.info(request, f"Bill {bill.reference} was posted to the GL first.")
    ap = Account.objects.filter(company=company, account_type="PAYABLE", is_active=True).order_by("code").first()

    # The cash side is chosen, not guessed: money leaves a specific account.
    bank = None
    raw_bank = request.POST.get("gl_account") or ""
    if raw_bank:
        bank = Account.objects.filter(
            pk=raw_bank, company=company, account_type__in=["BANK", "CASH"], is_active=True
        ).first()
        if not bank:
            messages.error(request, "That payment account does not belong to this company.")
            return redirect("procurement:bill_detail", pk=pk)
    else:
        bank = Account.objects.filter(
            company=company, account_type__in=["BANK", "CASH"], is_active=True
        ).order_by("code").first()
    if not (ap and bank):
        messages.error(request, "Chart of accounts missing a Payable or Bank/Cash account.")
        return redirect("procurement:bill_detail", pk=pk)

    payment_date = datetime.date.today()
    raw_date = request.POST.get("payment_date") or ""
    if raw_date:
        try:
            payment_date = datetime.datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            messages.error(request, "Payment date must be a real date.")
            return redirect("procurement:bill_detail", pk=pk)

    # Payables leave the book at the value the bill was raised at; the cash
    # goes out at the payment day's rate, and the gap between them is real money.
    from apps.accounting.currency import document_rate
    from apps.accounting.gl_service import _book_exchange_difference
    from apps.accounting.models import ExchangeRate, to_base
    from django.core.exceptions import ValidationError

    try:
        bill_rate = document_rate(bill, bill.bill_date)
        payment_rate = ExchangeRate.rate_for(company, bill.currency, payment_date)
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect("procurement:bill_detail", pk=pk)
    ap_base = to_base(amount, bill_rate)
    bank_base = to_base(amount, payment_rate)

    with transaction.atomic():
        ref = SequenceCounter.next_number(company, "JE-SPAY-")
        je = JournalEntry.objects.create(
            company=company,
            reference=ref,
            entry_type="PAYMENT",
            posting_date=payment_date,
            notes=f"Supplier payment for bill {bill.reference}",
            source_currency=bill.currency,
            exchange_rate=payment_rate,
        )
        JournalEntryLine.objects.create(
            journal_entry=je, account=ap,
            description=f"Pay {bill.supplier.name} — {bill.reference}",
            debit_amount=ap_base, credit_amount=Decimal("0"),
        )
        JournalEntryLine.objects.create(
            journal_entry=je, account=bank,
            description=f"Pay {bill.supplier.name} — {bill.reference}",
            debit_amount=Decimal("0"), credit_amount=bank_base,
        )
        _book_exchange_difference(
            je, company, ap_base - bank_base,
            f"Realised exchange on {bill.reference}")
        je.post(user=request.user)
        bill.paid_amount = (bill.paid_amount or Decimal("0")) + amount
        bill.outstanding_amount = bill.total_amount - bill.paid_amount
        if bill.outstanding_amount <= 0:
            bill.status = "PAID"
        else:
            bill.status = "PARTIAL"
        bill.save(update_fields=["paid_amount", "outstanding_amount", "status"])
    messages.success(request, f"Payment {amount} recorded for {bill.reference}. GL entry {je.reference} posted.")
    return redirect("procurement:bill_detail", pk=pk)


@login_required
@require_POST
def po_send(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk, company=request.active_company)
    if po.status != "DRAFT":
        messages.error(request, f"PO {po.reference} is {po.get_status_display()}, cannot send.")
    else:
        po.status = "SENT"
        po.save(update_fields=["status"])
        messages.success(request, f"PO {po.reference} marked as sent.")
    return redirect("procurement:po_detail", pk=pk)


@login_required
@require_POST
def po_cancel(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk, company=request.active_company)
    if po.status in ("BILLED", "FULLY_RECEIVED"):
        messages.error(request, f"PO {po.reference} is {po.get_status_display()}, cannot cancel.")
    elif po.bills.exclude(status="CANCELLED").exists():
        messages.error(
            request,
            f"Cannot cancel {po.reference}: bills have been raised against it. "
            "Cancel those first — they are the money side of the order.")
    else:
        po.status = "CANCELLED"
        po.save(update_fields=["status", "updated_at"])
        messages.success(request, f"PO {po.reference} cancelled.")
    return redirect("procurement:po_detail", pk=pk)


@login_required
@require_POST
def requisition_approve(request, pk):
    req = get_object_or_404(PurchaseRequisition, pk=pk, company=request.active_company)
    if req.status != "PENDING_APPROVAL":
        messages.error(request, f"Requisition {req.reference} is {req.get_status_display()}.")
    else:
        from django.utils import timezone
        req.status = "APPROVED"
        req.approved_by = request.user
        req.approved_at = timezone.now()
        req.save(update_fields=["status", "approved_by", "approved_at"])
        messages.success(request, f"Requisition {req.reference} approved.")
    return redirect("procurement:requisition_detail", pk=pk)


@login_required
@require_POST
def requisition_reject(request, pk):
    req = get_object_or_404(PurchaseRequisition, pk=pk, company=request.active_company)
    req.status = "REJECTED"
    req.save(update_fields=["status"])
    messages.success(request, f"Requisition {req.reference} rejected.")
    return redirect("procurement:requisition_detail", pk=pk)
