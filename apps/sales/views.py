"""Sales views."""
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, View

from .forms import InvoiceForm, InvoiceLineFormSet, CustomerPaymentForm
from .models import Invoice, CustomerPayment


class InvoiceListView(LoginRequiredMixin, ListView):
    template_name = "sales/invoice_list.html"
    context_object_name = "invoices"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Invoice.objects.none()
        qs = Invoice.objects.filter(company=company).select_related("customer")
        status = self.request.GET.get("status")
        if status:
            qs = qs.filter(status=status)
        q = self.request.GET.get("q")
        if q:
            qs = qs.filter(Q(reference__icontains=q) | Q(customer__name__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Invoices"
        ctx["status_filter"] = self.request.GET.get("status", "")
        ctx["q"] = self.request.GET.get("q", "")
        ctx["status_choices"] = Invoice.STATUS_CHOICES
        company = self.request.active_company
        if company:
            from django.db.models import Sum
            ctx["total_outstanding"] = Invoice.objects.filter(
                company=company, status__in=["SENT", "PARTIAL", "OVERDUE"]
            ).aggregate(t=Sum("outstanding_amount"))["t"] or Decimal("0")
        return ctx


class InvoiceCreateView(LoginRequiredMixin, CreateView):
    model = Invoice
    form_class = InvoiceForm
    template_name = "sales/invoice_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if self.request.POST:
            ctx["line_formset"] = InvoiceLineFormSet(self.request.POST, prefix="lines")
        else:
            ctx["line_formset"] = InvoiceLineFormSet(prefix="lines")
        if company:
            from apps.inventory.models import Product
            ctx["products"] = list(Product.objects.filter(company=company, is_active=True).values(
                "id", "name", "selling_price", "sku"
            ))
        ctx["page_title"] = "New Invoice"
        return ctx

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        company = self.request.active_company
        from apps.core.models import SequenceCounter
        if not form.instance.reference:
            form.instance.reference = SequenceCounter.next_number(company, "INV-")
        ctx = self.get_context_data(form=form)
        formset = ctx["line_formset"]
        if formset.is_valid():
            with transaction.atomic():
                self.object = form.save()
                formset.instance = self.object
                formset.save()
                self._recalculate_totals(self.object)
                # Reload so totals are fresh
                self.object.refresh_from_db()
                # Post to GL
                self._post_to_gl(self.object)
            messages.success(self.request, f"Invoice '{self.object.reference}' created and posted.")
            return redirect("sales:invoice_detail", pk=self.object.pk)
        return self.render_to_response(ctx)

    def _recalculate_totals(self, invoice):
        lines = invoice.lines.all()
        subtotal = sum(line.line_total for line in lines)
        tax = sum(line.tax_amount for line in lines)
        invoice.subtotal = subtotal
        invoice.tax_amount = tax
        invoice.total_amount = subtotal + tax
        invoice.outstanding_amount = invoice.total_amount - invoice.paid_amount
        Invoice.objects.filter(pk=invoice.pk).update(
            subtotal=subtotal, tax_amount=tax,
            total_amount=subtotal + tax,
            outstanding_amount=subtotal + tax - invoice.paid_amount,
        )

    def _post_to_gl(self, invoice):
        """Attempt GL posting; warn but don't fail if chart of accounts is incomplete."""
        try:
            from apps.accounting.gl_service import post_invoice
            post_invoice(invoice, user=self.request.user)
        except Exception as exc:
            messages.warning(
                self.request,
                f"Invoice saved, but GL posting was skipped: {exc}",
            )


class InvoiceDetailView(LoginRequiredMixin, DetailView):
    model = Invoice
    template_name = "sales/invoice_detail.html"
    context_object_name = "invoice"

    def get_queryset(self):
        return Invoice.objects.filter(company=self.request.active_company)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Invoice: {self.object.reference}"
        ctx["lines"] = self.object.lines.select_related("product", "tax_rate")
        ctx["payments"] = self.object.allocations.select_related("payment")
        return ctx


class InvoicePostView(LoginRequiredMixin, View):
    """
    POST-only view: (re-)post an invoice's journal entry.
    Useful for DRAFT invoices or when GL was skipped on creation.
    """

    def post(self, request, pk):
        company = request.active_company
        invoice = get_object_or_404(Invoice, pk=pk, company=company)

        # Clear existing journal entry link so post_invoice re-creates it
        if invoice.journal_entry_id:
            messages.info(request, "Invoice is already posted to GL.")
            return redirect("sales:invoice_detail", pk=pk)

        try:
            from apps.accounting.gl_service import post_invoice
            with transaction.atomic():
                post_invoice(invoice, user=request.user)
            messages.success(request, f"Invoice '{invoice.reference}' posted to GL.")
        except Exception as exc:
            messages.error(request, f"GL posting failed: {exc}")

        return redirect("sales:invoice_detail", pk=pk)


class CustomerPaymentListView(LoginRequiredMixin, ListView):
    template_name = "sales/payment_list.html"
    context_object_name = "payments"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return CustomerPayment.objects.none()
        return CustomerPayment.objects.filter(company=company).select_related("customer")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Customer Payments"
        return ctx


class CustomerPaymentCreateView(LoginRequiredMixin, CreateView):
    model = CustomerPayment
    form_class = CustomerPaymentForm
    template_name = "sales/payment_form.html"
    success_url = reverse_lazy("sales:payment_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        company = self.request.active_company
        from apps.core.models import SequenceCounter
        if not form.instance.reference:
            form.instance.reference = SequenceCounter.next_number(company, "PAY-")

        with transaction.atomic():
            self.object = form.save()
            self._post_to_gl(self.object)

        messages.success(self.request, f"Payment '{self.object.reference}' recorded.")
        return redirect(self.success_url)

    def _post_to_gl(self, payment):
        """Attempt GL posting; warn but don't fail if accounts are missing."""
        try:
            from apps.accounting.gl_service import post_customer_payment
            post_customer_payment(payment, user=self.request.user)
        except Exception as exc:
            messages.warning(
                self.request,
                f"Payment saved, but GL posting was skipped: {exc}",
            )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Record Payment"
        return ctx
