"""Procurement views."""
import datetime
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import CreateView, DetailView, ListView

from .models import Bill, PurchaseOrder


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
