"""Sales views."""
import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.utils.decorators import method_decorator
from django.views.generic import CreateView, DetailView, ListView, TemplateView, UpdateView, View

from apps.accounting.currency import book_currency, stamp_document
from apps.core.mixins import CompanyRequiredMixin
from apps.core.models import SequenceCounter

from .forms import InvoiceForm, InvoiceLineFormSet, CustomerPaymentForm, SalesOrderForm, SalesOrderLineFormSet
from .models import Invoice, InvoiceLine, CustomerPayment, SalesOrder, cents


# Larger than anything the columns can hold: quantities are stored with 8
# digits before the point, money with 13.
QUANTITY_LIMIT = Decimal("1e8")
MONEY_LIMIT = Decimal("1e13")


def _typed_number(raw, label, limit=QUANTITY_LIMIT):
    """What somebody typed into a number box, as a Decimal.

    Raises a ValidationError that says what is wrong with it otherwise. Python
    reads "NaN", "sNaN" and "Infinity" as numbers, but none of them is a
    quantity or an amount: NaN cannot even be compared with zero without an
    error, and stored in a money column it would turn every total after it
    into NaN. Figures too large for the column are refused here as well,
    rather than failing at the database.
    """
    text = "" if raw is None else str(raw).strip()
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValidationError(f"{label}: '{text}' is not a number.")
    if not value.is_finite() or abs(value) >= limit:
        raise ValidationError(f"{label}: '{text}' is not a usable figure.")
    return value


def _qty(value):
    """12.0000 written as 12 in a message."""
    return format(Decimal(str(value)), "f").rstrip("0").rstrip(".") or "0"


def _editable(invoice):
    """Only a draft that has never reached the ledger can be rewritten.

    Once an invoice is posted its entry is the record of what was billed; a
    change of lines, customer or currency on the paper would leave AR and VAT
    saying something else. A posted invoice is put right by a credit note or a
    cancellation, which both leave their own entry behind.
    """
    return invoice.status == "DRAFT" and not invoice.journal_entry_id


def _resync_order(invoice):
    """Bring the order an invoice was raised from back in line with its invoices."""
    if not invoice.sales_order_id:
        return
    order = SalesOrder.objects.select_for_update().filter(pk=invoice.sales_order_id).first()
    if order:
        order.sync_invoiced_quantities()


def _recalculate_invoice_totals(invoice):
    """Recompute header totals from the stored lines, which are the source of truth.

    Rounded to the cent here, once, the way the paper is printed: goods and tax
    each to the cent, the total their sum. The ledger posts these same figures,
    and the book-currency value worked out from them in save() is then the
    same on every later save — it is not computed from a total with unrounded
    tails that the database only trims afterwards.
    """
    lines = invoice.lines.all()
    invoice.subtotal = cents(sum((line.line_total for line in lines), Decimal("0")))
    invoice.tax_amount = cents(sum((line.tax_amount for line in lines), Decimal("0")))
    invoice.total_amount = invoice.subtotal + invoice.tax_amount
    # Saved through the model rather than a bulk update: the book-currency value
    # of a foreign invoice is only true once these totals are, and that is the
    # model's job to keep in step.
    invoice.save()


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
            ctx["book"] = book_currency(company)
            # Added in the book's money: the list may hold a dollar invoice and a
            # shilling invoice, and their paper amounts are not one total.
            ctx["total_outstanding"] = sum(
                (inv.outstanding_base for inv in Invoice.objects.filter(
                    company=company, status__in=["SENT", "PARTIAL", "OVERDUE"],
                    outstanding_amount__gt=Decimal("0"))), Decimal("0"))
        return ctx


class InvoiceCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    model = Invoice
    form_class = InvoiceForm
    template_name = "sales/invoice_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        form_kwargs = {"company": self.request.active_company}
        if self.request.POST:
            ctx["line_formset"] = InvoiceLineFormSet(
                self.request.POST, prefix="lines", form_kwargs=form_kwargs
            )
        else:
            ctx["line_formset"] = InvoiceLineFormSet(prefix="lines", form_kwargs=form_kwargs)
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
                _recalculate_invoice_totals(self.object)
                # Reload so totals are fresh
                self.object.refresh_from_db()
                # Post to GL
                posted = self._post_to_gl(self.object)
            if posted:
                messages.success(self.request, f"Invoice '{self.object.reference}' created and posted to the GL.")
            else:
                messages.info(
                    self.request,
                    f"Invoice '{self.object.reference}' is still DRAFT. Use Post to GL once the issue above is fixed.",
                )
            return redirect("sales:invoice_detail", pk=self.object.pk)
        return self.render_to_response(ctx)

    def _post_to_gl(self, invoice):
        """Post to GL, surfacing failures: a silent skip would leave an
        invoice that looks complete but has no ledger entry."""
        try:
            from apps.accounting.gl_service import post_invoice
            post_invoice(invoice, user=self.request.user)
            return True
        except Exception as exc:
            messages.error(
                self.request,
                f"Invoice '{invoice.reference}' saved, but GL posting failed: {exc}",
            )
            return False


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
        return CustomerPayment.objects.filter(company=company).select_related(
            "customer", "bank_account", "journal_entry").prefetch_related(
            "allocations__invoice")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Customer Payments"
        ctx["book"] = book_currency(self.request.active_company)
        for payment in ctx["payments"]:
            # How much of the receipt found a paper, and what is still just
            # money sitting on the account.
            rows = list(payment.allocations.all())
            payment.settled_count = len(rows)
            payment.settled_amount = sum(
                (a.allocated_amount for a in rows), Decimal("0"))
            payment.unapplied = payment.amount - payment.settled_amount
        return ctx


class CustomerPaymentCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    """Take money in, and say which of the customer's papers it settles.

    A receipt with nothing written against it is still a receipt: it clears the
    oldest open paper in the same money, which is what a running account usually
    means. The boxes below are for the times it does not — the customer says "this
    is against the March delivery" and the book has to be able to say that back a
    year later. So an allocation is checked before anything is saved: it has to
    name a paper of this customer, in this money, owed no less than it is paid,
    and the boxes together cannot take more than the money that came in.
    """
    model = CustomerPayment
    form_class = CustomerPaymentForm
    template_name = "sales/payment_form.html"
    success_url = reverse_lazy("sales:payment_list")

    ON_ACCOUNT = ["SENT", "PARTIAL", "OVERDUE"]

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def chosen_customer(self):
        """Whose papers to show — from the post, or from the strip above the form."""
        company = self.request.active_company
        pk = (self.request.POST.get("customer")
              or self.request.GET.get("customer") or "")
        if not company or not pk:
            return None
        from apps.crm.models import Customer
        try:
            return Customer.objects.filter(company=company, pk=pk).first()
        except ValidationError:
            return None

    def open_invoices(self, customer):
        if not customer:
            return []
        return Invoice.objects.filter(
            company=self.request.active_company, customer=customer,
            status__in=self.ON_ACCOUNT, outstanding_amount__gt=Decimal("0"),
        ).order_by("invoice_date")

    def typed_figures(self):
        """The settle-now boxes that hold a number, as (invoice pk, amount).

        An empty box, or a box left at zero, is not an allocation — it is the
        reader saying nothing about that paper. Anything else has to be a figure
        that means money coming in.
        """
        figures = []
        for key, text in self.request.POST.items():
            if not key.startswith("allocate_"):
                continue
            text = str(text).strip()
            if not text:
                continue
            try:
                amount = _typed_number(text, "Settled amount", MONEY_LIMIT)
            except ValidationError:
                return None, (f"'{text}' typed against one of the papers is not an "
                              "amount — the settled column takes a figure, not words.")
            if amount != amount.quantize(Decimal("0.01")):
                return None, (f"{text} typed against one of the papers is finer than a "
                              "cent; money is settled to the cent.")
            if amount < 0:
                return None, (f"{text} typed against one of the papers is money going "
                              "out; a settled paper is taken back by reversing the "
                              "receipt, not by typing a minus here.")
            if amount > 0:
                figures.append((key[len("allocate_"):], amount))
        return figures, None

    def read_allocations(self, form):
        """The figures typed against the customer's papers, or None to auto-settle.

        Returns (pairs, error). Blank boxes are not allocations, and a ticket with
        none at all hands the choice back to the service, which pays the oldest
        paper first — the ledger is never left holding a receipt that settles
        nothing and explains nothing either.
        """
        figures, error = self.typed_figures()
        if error:
            return None, error
        if not figures:
            return None, None
        wanted = {str(inv.pk): inv for inv in self.open_invoices(self.chosen_customer())}
        cleaned = form.cleaned_data
        pairs, total = [], Decimal("0")
        for pk, amount in figures:
            invoice = wanted.get(str(pk))
            if invoice is None:
                return None, (f"{amount} was typed against a paper this customer does "
                              "not owe — only the invoices listed above can be settled.")
            if amount > invoice.outstanding_amount:
                return None, (f"{invoice.reference} is only owed "
                              f"{invoice.outstanding_amount} — it cannot take {amount}.")
            if invoice.currency != cleaned.get("currency"):
                return None, (f"{invoice.reference} is billed in {invoice.currency} and "
                              f"this receipt is in {cleaned.get('currency')}: the two "
                              "only add up once one of them is converted.")
            total += amount
            pairs.append((invoice, amount))
        if total > cleaned["amount"]:
            return None, (f"The papers add up to {total} but only {cleaned['amount']} "
                          "was received — the rest would be a receipt for money nobody paid.")
        return pairs, None

    def form_valid(self, form):
        company = self.request.active_company
        allocations, error = self.read_allocations(form)
        if error:
            form.add_error(None, error)
            return self.form_invalid(form)

        form.instance.company = company
        if not form.instance.reference:
            form.instance.reference = SequenceCounter.next_number(company, "PAY-")

        with transaction.atomic():
            self.object = form.save()
            posted = self._post_to_gl(self.object, allocations)

        if posted:
            settled = (f" settled against {len(allocations)} paper(s)"
                       if allocations else " applied to the oldest open papers")
            messages.success(
                self.request,
                f"Payment '{self.object.reference}' recorded and posted to the GL —"
                f"{settled}.")
        return redirect(self.success_url)

    def _post_to_gl(self, payment, allocations=None):
        """Post to GL, surfacing failures instead of silently skipping."""
        try:
            from apps.accounting.gl_service import post_customer_payment
            post_customer_payment(payment, user=self.request.user,
                                  invoice_allocations=allocations)
            return True
        except Exception as exc:
            messages.error(
                self.request,
                f"Payment '{payment.reference}' saved, but GL posting failed: {exc}",
            )
            return False

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        customer = self.chosen_customer()
        typed = (self.request.POST if self.request.method == "POST" else {})
        ctx["page_title"] = "Record Payment"
        ctx["book"] = book_currency(company) if company else ""
        ctx["chosen"] = customer
        ctx["allocation_rows"] = [
            {"invoice": inv, "value": typed.get(f"allocate_{inv.pk}", "")}
            for inv in self.open_invoices(customer)]
        ctx["payers"] = []
        if company:
            from apps.crm.models import Customer
            owed = Invoice.objects.filter(
                company=company, status__in=self.ON_ACCOUNT,
                outstanding_amount__gt=Decimal("0")).values_list("customer_id", flat=True)
            ctx["payers"] = Customer.objects.filter(
                company=company, pk__in=list(set(owed)),
                is_active=True).order_by("name")
        return ctx


@login_required
@require_POST
def payment_reverse(request, pk):
    """Give a receipt back: the bank sent the money away, so the book must say so.

    A bounced cheque, a chargeback, a receipt typed against the wrong customer —
    none of them are erased. The answer is an offsetting entry in the original's
    own period, the invoices that receipt had settled take the slice back off and
    become owed again, and the allocation rows stay as the record of what was
    settled and then undone. `status` is what stops the same receipt being given
    back twice.
    """
    company = request.active_company
    payment = get_object_or_404(
        CustomerPayment.objects.filter(company=company).select_related(
            "customer", "journal_entry"), pk=pk)
    if payment.status == "REVERSED":
        messages.error(
            request, f"{payment.reference} has already been given back.")
        return redirect("sales:payment_list")
    if not payment.journal_entry_id:
        messages.error(
            request, f"{payment.reference} was never posted, so there is nothing in "
            "the book to reverse.")
        return redirect("sales:payment_list")

    reason = (request.POST.get("reason") or "").strip()
    try:
        with transaction.atomic():
            original = payment.journal_entry
            reversal = original.reverse(user=request.user)
            if reason:
                reversal.notes = f"Reversal of {original.reference} — {reason}"
                reversal.save(update_fields=["notes", "updated_at"])
            settled = payment.allocations.select_related("invoice")
            for allocation in settled:
                invoice = allocation.invoice
                invoice.paid_amount = max(
                    Decimal("0"),
                    (invoice.paid_amount or Decimal("0")) - allocation.allocated_amount)
                # save() works the outstanding and the name back out of it, so a
                # paper that was called paid is owed again.
                invoice.save()
            payment.status = "REVERSED"
            payment.save(update_fields=["status", "updated_at"])
    except ValidationError as exc:
        messages.error(
            request, f"Could not reverse {payment.reference}: {exc.messages[0]}")
        return redirect("sales:payment_list")

    messages.success(
        request,
        f"{payment.reference} reversed through {reversal.reference}: "
        f"{len(settled)} invoice(s) put back on account.")
    return redirect("sales:payment_list")


class CustomerStatementView(LoginRequiredMixin, TemplateView):
    """Full AR statement for a single customer — opening balance,
    invoices, credit notes, payments, closing balance within a date range.

    A customer can be billed in more than one money, but a balance is one
    number, so the statement is added up in the book's currency: each row keeps
    the document's own figure and rate for the reader, while debit, credit and
    the running balance carry the value that entered the ledger.
    """
    template_name = "sales/customer_statement.html"

    # What the ledger has seen. A draft is not yet a receivable and a cancelled
    # document is netted out by its own offset, so neither belongs on a
    # statement a customer is asked to agree to.
    ON_ACCOUNT = ["SENT", "PARTIAL", "PAID", "OVERDUE", "CREDITED", "CREDIT_NOTE"]

    def get_context_data(self, **kwargs):
        from decimal import Decimal
        from apps.accounting.currency import book_currency, book_value
        from apps.crm.models import Customer
        from .models import CustomerPayment, Invoice
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        customer = get_object_or_404(Customer, pk=kwargs["pk"], company=company)
        book = book_currency(company)
        start = self.request.GET.get("from") or ""
        end = self.request.GET.get("to") or ""
        currency = (self.request.GET.get("currency") or "").upper()
        try:
            from_date = datetime.date.fromisoformat(start) if start else None
            to_date = datetime.date.fromisoformat(end) if end else None
        except ValueError:
            from_date = to_date = None

        def invoices(statuses=None):
            qs = Invoice.objects.filter(
                customer=customer, status__in=(statuses or self.ON_ACCOUNT))
            if currency:
                qs = qs.filter(currency=currency)
            return qs

        def payments():
            # A receipt that was given back settled nothing and owes nothing, so
            # it stays out of the running balance; it is listed underneath as a
            # reversal instead, because it did happen.
            qs = CustomerPayment.objects.filter(customer=customer, status="RECEIVED")
            return qs.filter(currency=currency) if currency else qs

        def reversals():
            qs = CustomerPayment.objects.filter(customer=customer, status="REVERSED")
            return qs.filter(currency=currency) if currency else qs

        def in_window(queryset, field):
            if from_date:
                queryset = queryset.filter(**{f"{field}__gte": from_date})
            if to_date:
                queryset = queryset.filter(**{f"{field}__lte": to_date})
            return queryset

        def before_window(queryset, field):
            return queryset.filter(**{f"{field}__lt": from_date}) if from_date \
                else queryset.none()

        def row(date, kind, document, base, side, detail, url):
            """One line of the statement: the paper in its own money, the
            ledger effect in the book's."""
            credited = side == "credit"
            return {
                "date": date, "type": kind, "reference": document.reference,
                "detail": detail, "currency": document.currency,
                "paper": Decimal(str(getattr(document, "amount", None)
                                     or document.total_amount)),
                "rate": document.exchange_rate,
                "debit": Decimal("0") if credited else base,
                "credit": base if credited else Decimal("0"),
                "url": url,
            }

        def invoice_row(invoice):
            base = book_value(invoice)
            credited = (invoice.status == "CREDIT_NOTE"
                        or invoice.original_invoice_id is not None)
            return row(
                invoice.invoice_date,
                "Credit note" if credited else "Invoice", invoice, base,
                "credit" if credited else "debit",
                invoice.get_status_display(),
                reverse("sales:invoice_detail", args=[invoice.pk]))

        def payment_base(payment):
            return book_value(payment, "amount", "amount_base")

        window = [invoice_row(i) for i in
                  in_window(invoices(), "invoice_date")]
        window += [row(p.payment_date, "Payment", p, payment_base(p), "credit",
                       p.get_payment_method_display(), "")
                   for p in in_window(payments(), "payment_date")]
        window.sort(key=lambda r: (r["date"], str(r["reference"])))

        # Everything the book holds before the window, in the book's money.
        opening = sum(((invoice_row(i)["debit"] - invoice_row(i)["credit"])
                       for i in before_window(invoices(), "invoice_date")),
                      Decimal("0"))
        opening -= sum((payment_base(p)
                        for p in before_window(payments(), "payment_date")),
                       Decimal("0"))

        running = opening
        for r in window:
            running += r["debit"] - r["credit"]
            r["balance"] = running

        still_owed = sum(
            (i.outstanding_base for i in
             invoices(["SENT", "PARTIAL", "OVERDUE"])
             if i.outstanding_amount > 0), Decimal("0"))

        ctx["customer"] = customer
        ctx["book"] = book
        ctx["rows"] = window
        ctx["reversals"] = list(in_window(reversals(), "payment_date")
                                .select_related("journal_entry"))
        ctx["total_debit"] = sum((r["debit"] for r in window), Decimal("0"))
        ctx["total_credit"] = sum((r["credit"] for r in window), Decimal("0"))
        ctx["opening_balance"] = opening
        ctx["closing_balance"] = running
        ctx["still_owed"] = still_owed
        seen = {r["currency"] for r in window}
        ctx["currencies"] = sorted(
            seen | set(Invoice.objects.filter(customer=customer)
                       .values_list("currency", flat=True)))
        ctx["mixed"] = len(seen) > 1
        ctx["currency"] = currency
        ctx["from_date"] = start
        ctx["to_date"] = end
        ctx["start_on"] = from_date
        ctx["end_on"] = to_date
        return ctx


class ARAgingView(LoginRequiredMixin, TemplateView):
    """AR aging bucket summary across all customers."""
    template_name = "sales/ar_aging.html"

    def get_context_data(self, **kwargs):
        from datetime import date, timedelta
        from decimal import Decimal
        from apps.crm.models import Customer
        from .models import Invoice
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if not company:
            return ctx
        today = date.today()
        buckets = {"current": Decimal(0), "d30": Decimal(0), "d60": Decimal(0), "d90": Decimal(0), "d120": Decimal(0)}
        rows = []
        for cust in Customer.objects.filter(company=company):
            per = {"customer": cust, "current": Decimal(0), "d30": Decimal(0), "d60": Decimal(0), "d90": Decimal(0), "d120": Decimal(0), "total": Decimal(0)}
            for inv in Invoice.objects.filter(customer=cust, status__in=["SENT", "PARTIAL", "OVERDUE"]):
                # Bucketed in the book's own money: a dollar owed and a shilling
                # owed cannot be added together and called a receivable.
                out = inv.outstanding_base
                due = inv.due_date or inv.invoice_date
                age = (today - due).days
                if age <= 0: key = "current"
                elif age <= 30: key = "d30"
                elif age <= 60: key = "d60"
                elif age <= 90: key = "d90"
                else: key = "d120"
                per[key] += out
                per["total"] += out
                buckets[key] += out
            if per["total"] > 0:
                rows.append(per)
        ctx["rows"] = rows
        ctx["buckets"] = buckets
        ctx["book"] = (company.currency or "").upper()
        return ctx


class SalesOrderListView(LoginRequiredMixin, ListView):
    model = SalesOrder
    template_name = "sales/order_list.html"
    context_object_name = "orders"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return self.model.objects.none()
        return self.model.objects.filter(company=company).select_related("customer")


class SalesOrderCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    model = SalesOrder
    form_class = SalesOrderForm
    template_name = "sales/order_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_line_formset(self):
        form_kwargs = {"company": self.request.active_company}
        if self.request.POST:
            return SalesOrderLineFormSet(self.request.POST, prefix="lines", form_kwargs=form_kwargs)
        return SalesOrderLineFormSet(prefix="lines", form_kwargs=form_kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["line_formset"] = kwargs.pop("line_formset", None) or self.get_line_formset()
        ctx["page_title"] = "New Sales Order"
        return ctx

    def form_valid(self, form):
        company = self.request.active_company
        form.instance.company = company
        if not form.instance.reference:
            from apps.core.models import SequenceCounter
            form.instance.reference = SequenceCounter.next_number(company, "SO-")
        formset = self.get_line_formset()
        if not formset.is_valid():
            return self.render_to_response(self.get_context_data(form=form, line_formset=formset))
        with transaction.atomic():
            self.object = form.save()
            formset.instance = self.object
            formset.save()
            self.object.calculate_totals()
        messages.success(
            self.request,
            f"Sales order '{self.object.reference}' created — total {self.object.total_amount}.",
        )
        return redirect(self.get_success_url())

    def form_invalid(self, form):
        return self.render_to_response(self.get_context_data(form=form))

    def get_success_url(self):
        from django.urls import reverse
        return reverse("sales:order_detail", args=[self.object.pk])


class SalesOrderDetailView(LoginRequiredMixin, DetailView):
    model = SalesOrder
    template_name = "sales/order_detail.html"
    context_object_name = "order"

    def get_queryset(self):
        return SalesOrder.objects.filter(company=self.request.active_company).select_related("customer")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["invoices"] = self.object.invoices.select_related("customer")
        return ctx


class SalesDeliveryView(LoginRequiredMixin, DetailView):
    """Goods Issue Note — view & record what went out against a sales order."""
    model = SalesOrder
    template_name = "sales/order_deliver.html"
    context_object_name = "order"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return SalesOrder.objects.none()
        return SalesOrder.objects.filter(company=company)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["warehouses"] = self.object.company.warehouses.filter(is_active=True).order_by("name")
        ctx["book"] = book_currency(self.object.company)
        return ctx


class SalesIndexView(LoginRequiredMixin, TemplateView):
    """AR module home — a KPI dashboard summarising receivables."""
    template_name = "sales/index.html"

    def get_context_data(self, **kwargs):
        from decimal import Decimal
        from datetime import date
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if not company:
            return ctx
        from .models import Invoice, CustomerPayment, SalesOrder
        from apps.accounting.currency import book_value
        invs = Invoice.objects.filter(company=company)
        today = date.today()
        # Every KPI is read in the book's money — the papers may be in several.
        ctx["book"] = book_currency(company)
        open_invoices = invs.filter(status__in=["SENT", "PARTIAL", "OVERDUE"],
                                    outstanding_amount__gt=Decimal(0))
        ctx["total_outstanding"] = sum(
            (i.outstanding_base for i in open_invoices), Decimal(0))
        ctx["total_overdue"] = sum(
            (i.outstanding_base for i in open_invoices if i.due_date < today),
            Decimal(0))
        ctx["total_paid_30d"] = sum(
            (book_value(p, "amount", "amount_base") for p in
             CustomerPayment.objects.filter(
                 company=company, status="RECEIVED",
                 payment_date__gte=today.replace(day=1))),
            Decimal(0))
        ctx["open_orders_count"] = SalesOrder.objects.filter(company=company, status__in=["DRAFT", "CONFIRMED", "PARTIALLY_DELIVERED"]).count()
        ctx["recent_invoices"] = invs.order_by("-invoice_date")[:8]
        return ctx


# ------------------------- AR actions -------------------------


class InvoiceUpdateView(LoginRequiredMixin, UpdateView):
    """Invoice edit — reuses InvoiceForm, for drafts only.

    A draft is a working paper and can be rewritten freely. Anything that has
    reached the ledger — sent, part paid, paid, credited, cancelled — opens
    read-only with a note saying how it is corrected instead, and a change
    posted to it is refused: its entry would no longer match the paper.
    """
    model = Invoice
    form_class = InvoiceForm
    template_name = "sales/invoice_form.html"

    def get_queryset(self):
        return Invoice.objects.filter(company=self.request.active_company)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def locked_message(self, invoice):
        return (f"Invoice {invoice.reference} is {invoice.get_status_display()} and "
                "already in the ledger, so it cannot be edited. Issue a credit note "
                "for what is wrong, or cancel it and raise a new invoice.")

    def dispatch(self, request, *args, **kwargs):
        obj = self.get_object()
        if not _editable(obj):
            if request.method == "POST":
                messages.error(request, self.locked_message(obj))
                return redirect("sales:invoice_detail", pk=obj.pk)
            messages.warning(request, self.locked_message(obj))
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        from django.urls import reverse
        return reverse("sales:invoice_detail", args=[self.object.pk])

    def get_line_formset(self):
        form_kwargs = {"company": self.request.active_company}
        if self.request.POST:
            return InvoiceLineFormSet(
                self.request.POST, instance=self.object, prefix="lines", form_kwargs=form_kwargs
            )
        return InvoiceLineFormSet(instance=self.object, prefix="lines", form_kwargs=form_kwargs)

    def form_valid(self, form):
        formset = self.get_line_formset()
        if not formset.is_valid():
            return self.render_to_response(self.get_context_data(form=form, line_formset=formset))
        with transaction.atomic():
            # Checked again under a lock: the draft may have been posted from
            # another screen between opening this form and saving it.
            current = Invoice.objects.select_for_update().get(pk=self.object.pk)
            if not _editable(current):
                messages.error(self.request, self.locked_message(current))
                return redirect("sales:invoice_detail", pk=current.pk)
            self.object = form.save()
            formset.instance = self.object
            formset.save()
            _recalculate_invoice_totals(self.object)
            self.object.refresh_from_db()
            # A draft raised from an order bills exactly what its lines say;
            # the order has to hear about a quantity cut or a line removed.
            _resync_order(self.object)
        messages.success(self.request, f"Invoice {form.instance.reference} updated.")
        return redirect(self.get_success_url())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit Invoice {self.object.reference}"
        ctx["is_edit"] = True
        ctx["line_formset"] = kwargs.pop("line_formset", None) or self.get_line_formset()
        if not _editable(self.object):
            # Shown for reading only; the post is refused in dispatch().
            ctx["locked"] = True
            for field in ctx["form"].fields.values():
                field.disabled = True
            for line_form in ctx["line_formset"].forms:
                for field in line_form.fields.values():
                    field.disabled = True
        return ctx


@login_required
@require_POST
def invoice_cancel(request, pk):
    """Withdraw an invoice.

    An invoice that never reached the ledger is simply called off. One that did
    is answered by an offsetting entry — the original stays posted, exactly as
    it was written, because a book that gets rewritten is no longer evidence.
    Money that has already moved against it cannot be un-moved here; that is a
    payment reversal, not a cancellation.
    """
    inv = get_object_or_404(Invoice, pk=pk, company=request.active_company)
    if inv.status == "CANCELLED":
        messages.info(request, f"Invoice {inv.reference} is already cancelled.")
        return redirect("sales:invoice_detail", pk=pk)
    if inv.paid_amount > 0 or inv.credited_amount > 0:
        messages.error(
            request,
            f"Cannot cancel {inv.reference}: {inv.paid_amount} paid and "
            f"{inv.credited_amount} credited against it. Reverse the payment or "
            "issue a credit note for the part that is wrong.",
        )
        return redirect("sales:invoice_detail", pk=pk)

    with transaction.atomic():
        if inv.journal_entry_id:
            try:
                inv.journal_entry.reverse(user=request.user)
            except Exception as exc:
                messages.error(request, f"Cancel failed: {exc}")
                return redirect("sales:invoice_detail", pk=pk)
        inv.status = "CANCELLED"
        inv.save(update_fields=["status", "updated_at"])
        _resync_order(inv)

    messages.success(
        request,
        f"Invoice {inv.reference} cancelled."
        + (" Offsetting entry posted." if inv.journal_entry_id else ""),
    )
    return redirect("sales:invoice_detail", pk=pk)


# ───────────────────────── document lifecycle ─────────────────────────
#
# An order is a promise, an invoice is money. Converting one into the other
# therefore has to be traceable both ways: the order remembers how much of it
# has been billed, and the invoice remembers the order it came from.


@login_required
@require_POST
def order_confirm(request, pk):
    order = get_object_or_404(SalesOrder, pk=pk, company=request.active_company)
    if order.status != "DRAFT":
        messages.error(
            request,
            f"Order {order.reference} is {order.get_status_display()}: only a draft can be confirmed.")
    else:
        order.status = "CONFIRMED"
        order.save(update_fields=["status", "updated_at"])
        messages.success(request, f"Order {order.reference} confirmed.")
    return redirect("sales:order_detail", pk=pk)


@login_required
@require_POST
def order_deliver(request, pk):
    """Issue goods out of the warehouse against a sales order.

    Delivering is what sells the stock: every line taken off the shelf is
    charged at the moving average cost held there and booked as cost of goods
    sold against Inventory. The invoice carries the revenue and is raised
    separately; the two sides agree because both are measured against the same
    order and the same quantities.
    """
    from apps.inventory.models import StockMovement
    from apps.organizations.models import Warehouse

    order = get_object_or_404(
        SalesOrder.objects.filter(company=request.active_company).select_related("customer"),
        pk=pk)
    if order.status in ("DRAFT", "CANCELLED"):
        messages.error(
            request,
            f"Order {order.reference} is {order.get_status_display()}: "
            "confirm it before issuing goods against it.")
        return redirect("sales:order_detail", pk=pk)

    warehouse = order.warehouse
    raw_wh = request.POST.get("warehouse") or ""
    if raw_wh:
        try:
            warehouse = Warehouse.objects.filter(pk=raw_wh, company=order.company).first()
        except (ValidationError, ValueError):
            # Not even the shape of a warehouse id — a tampered or stale form.
            warehouse = None
        if not warehouse:
            messages.error(request, "That warehouse does not belong to this company.")
            return redirect("sales:order_deliver", pk=pk)
    if not warehouse:
        warehouse = (Warehouse.objects.filter(company=order.company, is_default=True).first()
                     or Warehouse.objects.filter(company=order.company).first())
    if not warehouse:
        messages.error(request, "This company has no warehouse to issue goods from.")
        return redirect("sales:order_deliver", pk=pk)

    wanted = []
    for line in order.lines.select_related("product"):
        raw = request.POST.get(f"deliver_qty_{line.pk}")
        if raw in (None, ""):
            continue  # an empty box means nothing left the shelf for that line
        try:
            qty = _typed_number(raw, f"Quantity of {line.product.name}")
        except ValidationError:
            messages.error(request, f"'{raw}' is not a quantity.")
            return redirect("sales:order_deliver", pk=pk)
        if qty < 0:
            messages.error(request, "Delivered quantities cannot be negative.")
            return redirect("sales:order_deliver", pk=pk)
        outstanding = line.quantity - (line.delivered_quantity or Decimal("0"))
        if qty > outstanding:
            messages.error(
                request,
                f"Cannot deliver {qty} of {line.product.name}: only {outstanding} "
                "of this line is still outstanding.")
            return redirect("sales:order_deliver", pk=pk)
        if qty > 0:
            wanted.append((line, qty))
    if not wanted:
        messages.error(request, "Enter at least one quantity to deliver.")
        return redirect("sales:order_deliver", pk=pk)

    today = datetime.date.today()
    try:
        with transaction.atomic():
            for line, qty in wanted:
                if line.product.track_inventory:
                    # Left at no cost on purpose: the movement takes the value
                    # off the shelf at the average the shelf itself holds, which
                    # is what the goods actually cost this company.
                    StockMovement.objects.create(
                        product=line.product, warehouse=warehouse,
                        movement_type="DELIVERY", quantity=qty,
                        sales_order=order,
                        movement_date=today, reference=order.reference,
                        performed_by=request.user,
                        notes=f"Delivered against {order.reference}",
                    )
                line.delivered_quantity = (line.delivered_quantity or Decimal("0")) + qty
                line.save(update_fields=["delivered_quantity"])
            # An order already billed keeps saying so: delivery tells the
            # warehouse what went out, not what the customer was charged for.
            if order.status != "INVOICED":
                delivered_all = all(
                    l.delivered_quantity >= l.quantity for l in order.lines.all())
                order.status = "DELIVERED" if delivered_all else "PARTIALLY_DELIVERED"
                order.save(update_fields=["status", "updated_at"])
    except ValidationError as exc:
        messages.error(
            request,
            f"Could not deliver against {order.reference}: {exc.messages[0]}")
        return redirect("sales:order_deliver", pk=pk)

    messages.success(
        request,
        f"Delivered {len(wanted)} line(s) from {warehouse.name}. The cost held on "
        "those shelves has been charged to cost of sales.")
    return redirect("sales:order_detail", pk=pk)


def _fold_discount(line_percent, order_percent):
    """An order-level discount expressed on the lines it applies to.

    The invoice has no header discount, so the two are multiplied out rather
    than added — otherwise a 10% order discount on a 10% line discount would
    bill 20% off instead of the 19% that was actually agreed.

    Kept exact: two percentages to two places multiply out to at most six, and
    the invoice line holds six. Rounding 5% then 2.5% (7.375%) to 7.38% made
    the invoice a few shillings short of the order it was raised from.
    """
    kept = (Decimal("100") - (line_percent or Decimal("0"))) * \
           (Decimal("100") - (order_percent or Decimal("0"))) / Decimal("100")
    folded = Decimal("100") - kept
    return min(max(folded.quantize(Decimal("0.000001")), Decimal("0")), Decimal("100"))


@login_required
@require_POST
def order_to_invoice(request, pk):
    """Convert the still-unbilled part of a sales order into a draft invoice."""
    order = get_object_or_404(
        SalesOrder.objects.filter(company=request.active_company).select_related("customer"),
        pk=pk)
    if order.status in ("DRAFT", "CANCELLED", "INVOICED"):
        messages.error(
            request,
            f"Order {order.reference} is {order.get_status_display()}: "
            + ("it is already invoiced — nothing is left to invoice."
               if order.status == "INVOICED" else
               "confirm it before invoicing."))
        return redirect("sales:order_detail", pk=pk)

    today = datetime.date.today()
    with transaction.atomic():
        # Locked while the uninvoiced quantities are read and taken, so a double
        # click raises one invoice and the second finds nothing left to bill.
        order = SalesOrder.objects.select_for_update().select_related("customer").get(pk=order.pk)
        if order.status in ("DRAFT", "CANCELLED", "INVOICED"):
            messages.error(
                request, f"Order {order.reference} is {order.get_status_display()}: "
                "nothing is left to invoice.")
            return redirect("sales:order_detail", pk=pk)
        lines = list(order.lines.select_related("product"))
        outstanding = [(l, l.quantity - (l.invoiced_quantity or Decimal("0"))) for l in lines]
        outstanding = [(l, q) for l, q in outstanding if q > 0]
        if not outstanding:
            messages.error(request, f"Order {order.reference} has nothing left to invoice.")
            return redirect("sales:order_detail", pk=pk)

        invoice = Invoice(
            company=order.company, customer=order.customer, sales_order=order,
            reference=SequenceCounter.next_number(order.company, "INV-"),
            invoice_date=today, due_date=today + datetime.timedelta(days=30),
            currency=order.currency, exchange_rate=order.exchange_rate,
            status="DRAFT", notes=f"Raised from order {order.reference}",
        )
        if invoice.currency != book_currency(order.company):
            # Carry the order's rate forward; if the order never had one, book
            # today's, so the invoice is never a document of unknown value.
            try:
                stamp_document(invoice, today)
            except ValidationError as exc:
                messages.error(request, exc.messages[0])
                return redirect("sales:order_detail", pk=pk)
        invoice.save()
        for line, qty in outstanding:
            InvoiceLine.objects.create(
                invoice=invoice,
                product=line.product,
                description=line.description or (line.product.name if line.product else "Item"),
                quantity=qty,
                unit_price=line.unit_price,
                discount_percent=_fold_discount(line.discount_percent, order.discount_percent),
                tax_rate=line.tax_rate,
                order_line=line,
            )
        _recalculate_invoice_totals(invoice)
        # The order's billed quantities are counted from its invoices, this
        # draft now among them.
        order.sync_invoiced_quantities()

    messages.success(
        request,
        f"Invoice {invoice.reference} raised from order {order.reference}. "
        "It is a draft — check it, then post it to the GL.")
    return redirect("sales:invoice_detail", pk=invoice.pk)


@login_required
@require_POST
def invoice_credit_note(request, pk):
    """Issue a credit note against this invoice, line by line.

    The quantities to credit are chosen on the original: whatever is entered
    becomes a copy of those lines on a new document, at the original's currency
    and booked rate. The invoice being credited is not touched — it keeps its
    own entry and gains a record of what has been credited against it.

    A unit can only be credited once. Each line's credit is capped at what it
    billed less what earlier credit notes already gave back, and the invoice is
    locked while that is worked out, so the same form sent twice cannot both
    pass the check and book two notes for one return.
    """
    original = get_object_or_404(
        Invoice.objects.filter(company=request.active_company).select_related("customer"), pk=pk)
    if not original.journal_entry_id:
        messages.error(
            request,
            f"{original.reference} has not been posted to the GL, so there is "
            "nothing to credit. Edit or cancel the draft instead.")
        return redirect("sales:invoice_detail", pk=pk)
    if original.status in ("CANCELLED", "CREDIT_NOTE"):
        messages.error(request, f"Cannot credit a {original.get_status_display().lower()}.")
        return redirect("sales:invoice_detail", pk=pk)

    typed = {}
    for line in original.lines.all():
        raw = request.POST.get(f"credit_qty_{line.pk}")
        if raw in (None, ""):
            continue
        try:
            qty = _typed_number(raw, f"Credit quantity of {line.description}")
        except ValidationError:
            messages.error(request, f"'{raw}' is not a quantity.")
            return redirect("sales:invoice_detail", pk=pk)
        if qty < 0:
            messages.error(request, "Credit quantities cannot be negative.")
            return redirect("sales:invoice_detail", pk=pk)
        if qty > 0:
            typed[line.pk] = qty
    if not typed:
        messages.error(request, "Enter at least one quantity to credit.")
        return redirect("sales:invoice_detail", pk=pk)

    today = datetime.date.today()
    with transaction.atomic():
        # Locked, then read again: a second submit of the same form waits here
        # and then sees the first note's quantities as already credited.
        original = Invoice.objects.select_for_update().select_related("customer").get(pk=original.pk)
        if not original.journal_entry_id or original.status in ("CANCELLED", "CREDIT_NOTE"):
            messages.error(
                request, f"{original.reference} is {original.get_status_display()}: "
                "there is nothing on it left to credit.")
            return redirect("sales:invoice_detail", pk=pk)
        credited = _already_credited(original)
        wanted = []
        for line in original.lines.select_related("product", "tax_rate"):
            qty = typed.get(line.pk)
            if not qty:
                continue
            left = line.quantity - credited.get(line.pk, Decimal("0"))
            if qty > left:
                done = credited.get(line.pk, Decimal("0"))
                messages.error(
                    request,
                    f"Cannot credit {_qty(qty)} of {line.description}: that is more than "
                    f"is left to credit. {original.reference} billed {_qty(line.quantity)}"
                    + (f" and earlier credit notes already gave back {_qty(done)}" if done else "")
                    + f", so at most {_qty(max(left, Decimal('0')))} can still be credited.")
                return redirect("sales:invoice_detail", pk=pk)
            wanted.append((line, qty))

        note = Invoice(
            company=original.company, customer=original.customer,
            sales_order_id=None, original_invoice=original,
            reference=SequenceCounter.next_number(original.company, "CN-"),
            invoice_date=today, due_date=today,
            currency=original.currency, exchange_rate=original.exchange_rate,
            status="CREDIT_NOTE",
            notes=request.POST.get("reason", "").strip() or f"Credit note against {original.reference}",
        )
        note.save()
        for line, qty in wanted:
            InvoiceLine.objects.create(
                invoice=note, product=line.product, description=line.description,
                quantity=qty, unit_price=line.unit_price,
                discount_percent=line.discount_percent, tax_rate=line.tax_rate,
                credited_line=line,
            )
        _recalculate_invoice_totals(note)
        note.refresh_from_db()

        if note.total_amount > original.outstanding_amount:
            # Crediting more than is owed would turn a receivable into a
            # payable through the wrong door — that is a refund, not a credit.
            transaction.set_rollback(True)
            messages.error(
                request,
                f"Credit ({note.total_amount} {note.currency}) is more than "
                f"{original.reference} still owes ({original.outstanding_amount} "
                f"{original.currency}). Refund the payment instead.")
            return redirect("sales:invoice_detail", pk=pk)

        try:
            from apps.accounting.gl_service import post_credit_note
            post_credit_note(note, user=request.user)
        except Exception as exc:
            transaction.set_rollback(True)
            messages.error(request, f"Credit note could not be posted: {exc}")
            return redirect("sales:invoice_detail", pk=pk)

        # The note settles the invoice it answers, and says so on both sides:
        # applied in full, so it never reads as money still owed to anyone.
        note.credited_amount = note.total_amount
        note.save(update_fields=["credited_amount", "outstanding_amount", "updated_at"])
        original.credited_amount = (original.credited_amount or Decimal("0")) + note.total_amount
        original.save()

    messages.success(
        request,
        f"Credit note {note.reference} for {note.total_amount} {note.currency} posted "
        f"against {original.reference}.")
    return redirect("sales:invoice_detail", pk=original.pk)


def _already_credited(original):
    """How much of each of the invoice's lines live credit notes have given back.

    Returned as {invoice line pk: quantity}. A note line names the invoice line
    it credits; notes written before that link existed are matched to the first
    line of the same product, description and price, which is what they were
    copied from. Cancelled notes give nothing back and are not counted.
    """
    lines = list(original.lines.all())
    credited = {line.pk: Decimal("0") for line in lines}
    note_lines = InvoiceLine.objects.filter(
        invoice__original_invoice=original, invoice__status="CREDIT_NOTE")
    for note_line in note_lines:
        target = note_line.credited_line_id
        if target not in credited:
            target = next(
                (line.pk for line in lines
                 if line.product_id == note_line.product_id
                 and line.description == note_line.description
                 and line.unit_price == note_line.unit_price), None)
        if target is not None:
            credited[target] += note_line.quantity or Decimal("0")
    return credited
