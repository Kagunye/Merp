"""Procurement views."""
import datetime
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, TemplateView, UpdateView, View

from .models import Bill, PurchaseOrder, PurchaseRequisition
from apps.core.doc_views import DocumentPrintMixin, DocumentEmailView, lines_from_queryset


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


class BillCreateView(LoginRequiredMixin, CreateView):
    model = Bill
    template_name = "procurement/bill_form.html"
    fields = ["reference", "supplier", "supplier_reference", "bill_date", "due_date", "subtotal", "tax_amount", "total_amount", "notes"]

    def get_success_url(self):
        from django.urls import reverse
        return reverse("procurement:bill_detail", kwargs={"pk": self.object.pk})

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
            else:
                field.widget.attrs["class"] = "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
        if company:
            from apps.crm.models import Supplier
            form.fields["supplier"].queryset = Supplier.objects.filter(company=company, is_active=True)
            from apps.core.models import SequenceCounter
            form.fields["reference"].initial = SequenceCounter.next_number(company, "BILL-")
        form.fields["bill_date"].initial = datetime.date.today()
        form.fields["due_date"].initial = datetime.date.today() + datetime.timedelta(days=30)
        return form

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        # Set outstanding_amount
        bill = form.save(commit=False)
        bill.outstanding_amount = bill.total_amount - bill.paid_amount
        bill.company = self.request.active_company
        bill.save()
        messages.success(self.request, f"Bill '{bill.reference}' created.")
        from django.urls import reverse
        return redirect(reverse("procurement:bill_detail", kwargs={"pk": bill.pk}))

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
    fields = ["reference", "supplier", "order_date", "expected_delivery_date", "warehouse", "subtotal", "tax_amount", "total_amount", "notes"]

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
        return ctx


class SupplierStatementView(LoginRequiredMixin, TemplateView):
    """Full AP statement for a supplier: bills, outstanding balance."""
    template_name = "procurement/supplier_statement.html"

    def get_context_data(self, **kwargs):
        from decimal import Decimal
        from apps.crm.models import Supplier
        from .models import Bill
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        supplier = get_object_or_404(Supplier, pk=kwargs["pk"], company=company)
        start = self.request.GET.get("from") or ""
        end = self.request.GET.get("to") or ""

        bills = Bill.objects.filter(supplier=supplier)
        if start: bills = bills.filter(bill_date__gte=start)
        if end: bills = bills.filter(bill_date__lte=end)

        rows = []
        running = Decimal(0)
        for b in bills.order_by("bill_date"):
            running += b.total_amount - b.paid_amount
            rows.append({
                "date": b.bill_date, "reference": b.reference,
                "due_date": b.due_date, "total": b.total_amount,
                "paid": b.paid_amount, "outstanding": b.outstanding_amount,
                "balance": running, "status": b.get_status_display(),
            })

        ctx["supplier"] = supplier
        ctx["rows"] = rows
        ctx["total_billed"] = sum(r["total"] for r in rows)
        ctx["total_paid"] = sum(r["paid"] for r in rows)
        ctx["closing_balance"] = running
        ctx["from_date"] = start
        ctx["to_date"] = end
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
                out = b.outstanding_amount
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
        ctx["total_outstanding"] = sum((b.outstanding_amount for b in bills.filter(status__in=["RECEIVED", "PARTIAL"])), Decimal(0))
        ctx["total_overdue"] = sum((b.outstanding_amount for b in bills.filter(status__in=["RECEIVED", "PARTIAL"], due_date__lt=today)), Decimal(0))
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


class GRNView(LoginRequiredMixin, DetailView):
    """Goods Receipt Note — view & record what came in against a PO."""
    model = PurchaseOrder
    template_name = "procurement/grn.html"
    context_object_name = "po"


# ------------------------- AP actions -------------------------


class BillUpdateView(LoginRequiredMixin, UpdateView):
    """Edit a bill. Blocks edits once fully paid or cancelled."""
    model = Bill
    template_name = "procurement/bill_form.html"
    fields = ["reference", "supplier", "supplier_reference", "bill_date", "due_date",
              "subtotal", "tax_amount", "total_amount", "notes"]

    def get_queryset(self):
        return Bill.objects.filter(company=self.request.active_company)

    def dispatch(self, request, *args, **kwargs):
        obj = self.get_object()
        if obj.status in ("PAID", "CANCELLED"):
            messages.error(request, f"Bill {obj.reference} cannot be edited ({obj.get_status_display()}).")
            return redirect("procurement:bill_detail", pk=obj.pk)
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
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
            else:
                field.widget.attrs["class"] = "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
        company = self.request.active_company
        if company:
            from apps.crm.models import Supplier
            form.fields["supplier"].queryset = Supplier.objects.filter(company=company, is_active=True)
        return form

    def get_success_url(self):
        from django.urls import reverse
        return reverse("procurement:bill_detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        messages.success(self.request, f"Bill {form.instance.reference} updated.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit Bill {self.object.reference}"
        ctx["is_edit"] = True
        return ctx


@login_required
@require_POST
def bill_cancel(request, pk):
    bill = get_object_or_404(Bill, pk=pk, company=request.active_company)
    if bill.paid_amount > 0:
        messages.error(request, f"Cannot cancel {bill.reference}: already has payments.")
    else:
        bill.status = "CANCELLED"
        bill.save(update_fields=["status"])
        messages.success(request, f"Bill {bill.reference} cancelled.")
    return redirect("procurement:bill_detail", pk=pk)


@login_required
@require_POST
def bill_pay(request, pk):
    """Record a supplier payment against a bill.

    Creates a posted JournalEntry (Dr AP, Cr Bank/Cash) and updates
    bill.paid_amount + status. Payment account is picked from the
    chart of accounts (first PAYABLE and first BANK/CASH).
    """
    from decimal import Decimal
    from django.utils import timezone
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
    ap = Account.objects.filter(company=company, account_type="PAYABLE", is_active=True).first()
    bank = Account.objects.filter(company=company, account_type__in=["BANK", "CASH"], is_active=True).first()
    if not (ap and bank):
        messages.error(request, "Chart of accounts missing a Payable or Bank/Cash account.")
        return redirect("procurement:bill_detail", pk=pk)

    with transaction.atomic():
        ref = SequenceCounter.next_number(company, "SPY-")
        je = JournalEntry.objects.create(
            company=company,
            reference=f"JE-{ref}",
            entry_type="PAYMENT",
            posting_date=bill.bill_date,
            notes=f"Supplier payment for bill {bill.reference}",
            status="POSTED",
            posted_at=timezone.now(),
        )
        JournalEntryLine.objects.create(
            journal_entry=je, account=ap,
            description=f"Pay {bill.supplier.name} — {bill.reference}",
            debit_amount=amount, credit_amount=Decimal("0"),
        )
        JournalEntryLine.objects.create(
            journal_entry=je, account=bank,
            description=f"Pay {bill.supplier.name} — {bill.reference}",
            debit_amount=Decimal("0"), credit_amount=amount,
        )
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
    else:
        po.status = "CANCELLED"
        po.save(update_fields=["status"])
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


# ============================================================
# Printable + emailable documents (PO, Bill)
# ============================================================

def _po_doc(obj):
    return {
        "doc_title": "Purchase Order",
        "doc_reference": obj.reference,
        "doc_date": obj.order_date,
        "doc_status": obj.get_status_display(),
        "doc_party_label": "Vendor",
        "doc_party": obj.supplier,
        "doc_meta": [
            ("Order Date", obj.order_date.strftime("%d %b %Y")),
            ("Expected", obj.expected_delivery_date.strftime("%d %b %Y") if obj.expected_delivery_date else "—"),
            ("Currency", obj.currency),
        ],
        "doc_lines": [
            {
                "description": line.description or (line.product.name if line.product else ""),
                "sku": line.product.sku if line.product else "",
                "qty": line.quantity,
                "unit_price": line.unit_cost,
                "line_total": line.line_total,
            }
            for line in obj.lines.all()
        ],
        "doc_subtotal": obj.subtotal,
        "doc_tax": obj.tax_amount,
        "doc_total": obj.total_amount,
        "doc_currency": obj.currency,
        "doc_notes": getattr(obj, "notes", ""),
        "doc_show_bank": False,
        "doc_back_url": reverse_lazy("procurement:po_detail", args=[obj.pk]),
        "doc_email_url": reverse_lazy("procurement:po_email", args=[obj.pk]),
    }


class POPrintView(DocumentPrintMixin, View):
    model = PurchaseOrder
    def _build_doc(self, obj):
        return _po_doc(obj)


class POEmailView(DocumentEmailView):
    model = PurchaseOrder
    print_url_name = "procurement:po_print"
    doc_label = "Purchase Order"
    def build_doc(self, obj):
        return _po_doc(obj)


def _bill_doc(obj):
    return {
        "doc_title": "Supplier Bill",
        "doc_reference": obj.reference,
        "doc_date": obj.bill_date,
        "doc_status": obj.get_status_display(),
        "doc_party_label": "From Supplier",
        "doc_party": obj.supplier,
        "doc_meta": [
            ("Bill Date", obj.bill_date.strftime("%d %b %Y")),
            ("Due Date", obj.due_date.strftime("%d %b %Y")),
            ("Supplier Ref", obj.supplier_reference or "—"),
            ("Currency", obj.currency),
        ],
        "doc_lines": lines_from_queryset(obj.lines.all()) if hasattr(obj, "lines") else [],
        "doc_subtotal": obj.subtotal,
        "doc_tax": obj.tax_amount,
        "doc_total": obj.total_amount,
        "doc_currency": obj.currency,
        "doc_notes": getattr(obj, "notes", ""),
        "doc_show_bank": False,
        "doc_back_url": reverse_lazy("procurement:bill_detail", args=[obj.pk]),
        "doc_email_url": reverse_lazy("procurement:bill_email", args=[obj.pk]),
    }


class BillPrintView(DocumentPrintMixin, View):
    model = Bill
    def _build_doc(self, obj):
        return _bill_doc(obj)


class BillEmailView(DocumentEmailView):
    model = Bill
    print_url_name = "procurement:bill_print"
    doc_label = "Supplier Bill"
    def build_doc(self, obj):
        return _bill_doc(obj)
