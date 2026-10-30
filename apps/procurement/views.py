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

from apps.accounting.currency import display_rate
from apps.core.mixins import CompanyRequiredMixin
from apps.inventory.numbers import MONEY, QUANTITY, UNIT_COST, fits, money, pick, typed_number
from apps.inventory.numbers import qty as show_qty

from .forms import BillForm, PurchaseOrderForm
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


def may_post_bills(request) -> bool:
    """Whether this user may put a bill on the ledger in the active company.

    Asked of the same access table that guards the Post button, so a role
    that cannot press Post cannot post by another road — saving a new bill,
    say — and the rule lives in one place.
    """
    from apps.core import access
    return access.can(request.user, getattr(request, "company_role", None),
                      "procurement", "bill_post", "POST")


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
        company = self.request.active_company
        if company:
            from apps.accounting.currency import book_currency
            ctx["book"] = book_currency(company)
            # Added in the book's money for the same reason as the invoice book:
            # a dollar bill and a shilling bill do not share a total.
            ctx["total_outstanding"] = sum(
                (b.outstanding_base for b in Bill.objects.filter(
                    company=company, status__in=["RECEIVED", "PARTIAL", "OVERDUE"],
                    outstanding_amount__gt=Decimal("0"))), Decimal("0"))
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
            # The next number in the series, skipping any a bookkeeper already
            # typed by hand: references are unique within the company.
            bill.reference = SequenceCounter.next_number(company, "BILL-")
            while Bill.objects.filter(company=company, reference__iexact=bill.reference).exists():
                bill.reference = SequenceCounter.next_number(company, "BILL-")
        bill.save()
        self.object = bill
        if not may_post_bills(self.request):
            # Entering a supplier's invoice and putting it on the ledger are two
            # different people's jobs. Whoever may not post a bill with the Post
            # button may not post one by saving it either: the bill waits as a
            # draft for finance to check against the paper and post.
            messages.success(
                self.request,
                f"Bill '{bill.reference}' saved as a draft for "
                f"{money(bill.total_amount)} {bill.currency}. Finance posts it to the GL "
                "once it has been checked against the supplier's invoice.")
            return redirect(self.get_success_url())
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
        company = self.request.active_company
        # Bank accounts first, and the main one chosen: suppliers are paid by
        # transfer or cheque. The till's cash is still on the list, below them,
        # for the rare payment made over the counter — but only on purpose.
        accounts = Account.objects.filter(
            company=company, account_type__in=["BANK", "CASH"], is_active=True)
        ctx["cash_accounts"] = (list(accounts.filter(account_type="BANK").order_by("code"))
                                + list(accounts.filter(account_type="CASH").order_by("code")))
        ctx["main_bank"] = main_bank_account(company)
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


class PurchaseOrderCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    """Raise a purchase order, in the supplier's currency.

    The currency and its rate are on the order from the start (see
    PurchaseOrderForm): a dollar order saved as shillings would cost its goods
    into stock at the dollar figure read as shillings, a hundred-odd times too
    cheap, and the bill raised from it would ask to pay that.
    """
    model = PurchaseOrder
    form_class = PurchaseOrderForm
    template_name = "procurement/po_form.html"

    def get_success_url(self):
        from django.urls import reverse
        return reverse("procurement:po_detail", kwargs={"pk": self.object.pk})

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

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
        if company and not form.is_bound:
            from apps.core.models import SequenceCounter
            # A preview: the number is taken at save time (claim_number), so
            # opening this form and leaving does not leave a gap in the series.
            form.fields["reference"].initial = SequenceCounter.peek_number(company, "PO-")
        form.fields["order_date"].initial = datetime.date.today()
        return form

    def form_valid(self, form):
        from apps.core.models import SequenceCounter
        company = self.request.active_company
        form.instance.company = company
        # The number shown on the form was only a preview; it is taken here.
        form.instance.reference = SequenceCounter.claim_number(
            company, "PO-", form.instance.reference,
            taken=lambda ref: PurchaseOrder.objects.filter(company=company, reference=ref).exists())
        response = super().form_valid(form)
        po = self.object
        rate = (f" at {po.exchange_rate.normalize():f}"
                if po.exchange_rate and po.exchange_rate != 1 else "")
        messages.success(
            self.request,
            f"Purchase Order '{po.reference}' created in {po.currency}{rate}.")
        return response

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
            # Money that left the account is in the bill's own currency, read
            # back at the rate the bill was raised at. Each payment and each
            # note took its share of the bill's book value as the balance
            # before it less the balance after it, so what the payments took
            # is the bill's value less what is still owed and what its notes
            # took — the same figure AP was debited with, to the cent.
            # Converting the paid total on its own would differ from it by the
            # cent that settling part by part puts right.
            if noted:
                paid = to_base(bill.paid_amount, display_rate(bill, bill.bill_date))
            else:
                notes = sum((book_value(n) for n in bill.debit_notes.filter(
                    status="DEBIT_NOTE")), Decimal("0"))
                paid = (base - bill.outstanding_base - notes) if bill.paid_amount else Decimal("0")
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
            for b in Bill.objects.filter(supplier=sup, status__in=["RECEIVED", "PARTIAL", "OVERDUE"]):
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


class PurchaseRequisitionCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    """Start a request to buy. It is saved as a draft with no lines yet; the
    lines are added on the requisition, which is then submitted for approval."""
    model = PurchaseRequisition
    template_name = "procurement/requisition_form.html"

    def get_form_class(self):
        from .forms import PurchaseRequisitionForm
        return PurchaseRequisitionForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        from apps.organizations.models import Department
        company = self.request.active_company
        form.fields["department"].queryset = (
            Department.objects.filter(company=company) if company else Department.objects.none())
        if company and not form.is_bound:
            from apps.core.models import SequenceCounter
            # A preview only, as on the order form: the number is taken when
            # the requisition is saved (claim_number), so opening this form and
            # walking away leaves no gap in the PR- series.
            form.fields["reference"].initial = SequenceCounter.peek_number(company, "PR-")
        return form

    def form_valid(self, form):
        from apps.core.models import SequenceCounter
        company = self.request.active_company
        form.instance.company = company
        form.instance.requested_by = self.request.user
        form.instance.reference = SequenceCounter.claim_number(
            company, "PR-", form.instance.reference,
            taken=lambda ref: PurchaseRequisition.objects.filter(
                company=company, reference=ref).exists())
        messages.success(
            self.request,
            f"Purchase requisition {form.instance.reference} created as a draft. Add what "
            "is needed below, then submit it for approval.")
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

    def get_context_data(self, **kwargs):
        from apps.core import access
        from apps.crm.models import Supplier
        from apps.inventory.models import Product
        ctx = super().get_context_data(**kwargs)
        company = self.object.company
        role = getattr(self.request, "company_role", None)
        user = self.request.user
        ctx["lines"] = self.object.lines.select_related("product", "product__unit_of_measure")
        ctx["products"] = Product.objects.filter(company=company, is_active=True).order_by("name")
        ctx["suppliers"] = Supplier.objects.filter(company=company, is_active=True).order_by("name")
        ctx["warehouses"] = company.warehouses.filter(is_active=True).order_by("name")
        ctx["orders"] = self.object.purchaseorder_set.exclude(status="CANCELLED")
        # Only the buttons this person may press are shown: approving is not
        # the requester's job, and a button that answers "not allowed" teaches
        # nobody anything.
        # Nor is approving one's own request (requisition_approve refuses it).
        ctx["can_decide"] = (
            access.can(user, role, "procurement", "requisition_approve", "POST")
            and user.pk not in {self.object.requested_by_id, self.object.submitted_by_id})
        ctx["can_raise_po"] = access.can(user, role, "procurement", "requisition_to_po", "POST")
        ctx["can_edit"] = access.can(user, role, "procurement", "requisition_submit", "POST")
        return ctx


def _requisition_for(request, pk):
    return get_object_or_404(PurchaseRequisition, pk=pk, company=request.active_company)


def _requisition_total(req):
    """The estimate the approver is asked to agree to, rounded once to the cent."""
    total = sum((l.estimated_total for l in req.lines.all()), Decimal("0"))
    req.total_amount = total.quantize(Decimal("0.01"))
    req.save(update_fields=["total_amount", "updated_at"])
    return req.total_amount


@login_required
@require_POST
def requisition_line_add(request, pk):
    """Add one product to a draft requisition.

    A requisition is changed only while it is a draft. Once it is with the
    approver, what they approve has to be what they were shown.

    Every line names a product. A requisition exists to become a purchase
    order, and an order line is a product the goods receipt can put on a shelf
    (or a service the bill can charge to its account); a line that only
    describes something could be approved and then never ordered, leaving the
    whole requisition stuck. Something the company has never bought is added
    as a product first — a service, if it is not kept in stock.
    """
    from apps.inventory.models import Product
    from apps.organizations.models import Warehouse
    from .models import PurchaseRequisitionLine

    req = _requisition_for(request, pk)
    back = redirect("procurement:requisition_detail", pk=pk)
    if req.status != "DRAFT":
        messages.error(
            request,
            f"Requisition {req.reference} is {req.state_phrase}, so its "
            "lines can no longer change. Only a draft can be edited.")
        return back

    raw_product = request.POST.get("product") or ""
    product = pick(Product.objects.filter(company=req.company, is_active=True), raw_product)
    if not product:
        typed = (request.POST.get("description") or "").strip()
        messages.error(
            request,
            ("Choose a product from this company for each line"
             + (f" — “{typed[:60]}” names none" if typed else "")
             + ". A line with no product could be approved but never ordered. If it is "
             "something new, add it as a product first (a service if it is not kept in "
             "stock), then add it here."))
        return back
    description = (request.POST.get("description") or "").strip() or product.name
    try:
        quantity = typed_number(request.POST.get("quantity"), "Quantity", QUANTITY)
        cost = typed_number(request.POST.get("estimated_unit_cost"), "Estimated unit cost",
                            UNIT_COST, allow_zero=True, blank=Decimal("0"))
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return back
    if quantity is None:
        messages.error(request, "Enter the quantity needed, a number greater than zero.")
        return back
    if not fits(quantity * cost, MONEY):
        messages.error(request, f"{show_qty(quantity)} × {money(cost)} is more than a "
                                "requisition line can hold. Check the figures.")
        return back
    warehouse = None
    raw_wh = request.POST.get("warehouse") or ""
    if raw_wh:
        warehouse = pick(Warehouse.objects.filter(company=req.company), raw_wh)
        if not warehouse:
            messages.error(request, "That warehouse does not belong to this company.")
            return back

    with transaction.atomic():
        PurchaseRequisitionLine.objects.create(
            requisition=req, product=product, description=description[:500],
            quantity=quantity, estimated_unit_cost=cost, warehouse=warehouse)
        total = _requisition_total(req)
    messages.success(request, f"{description} added to {req.reference} — estimated total "
                              f"{money(total)}.")
    return back


@login_required
@require_POST
def requisition_line_delete(request, pk, line_pk):
    from .models import PurchaseRequisitionLine

    req = _requisition_for(request, pk)
    line = get_object_or_404(PurchaseRequisitionLine, pk=line_pk, requisition=req)
    if req.status != "DRAFT":
        messages.error(request, f"Requisition {req.reference} is "
                                f"{req.state_phrase}; only a draft can change.")
    else:
        line.delete()
        _requisition_total(req)
        messages.success(request, f"{line.description} removed from {req.reference}.")
    return redirect("procurement:requisition_detail", pk=pk)


@login_required
@require_POST
def requisition_submit(request, pk):
    """Send a draft to the approver: DRAFT -> PENDING_APPROVAL.

    A requisition with nothing on it asks the approver to agree to nothing, so
    it needs at least one line; its estimated total is worked out from them
    here, once, so the approver sees the figure they are agreeing to.
    """
    req = _requisition_for(request, pk)
    loose = list(req.lines.filter(product__isnull=True).values_list("description", flat=True))
    if req.status != "DRAFT":
        messages.error(request, f"Requisition {req.reference} is "
                                f"{req.state_phrase}; only a draft can be submitted.")
    elif not req.lines.exists():
        messages.error(request, f"Requisition {req.reference} has nothing on it. Add at least "
                                "one line before submitting it for approval.")
    elif loose:
        # Lines entered before every line had to name a product: approved,
        # they could never be ordered.
        messages.error(
            request,
            f"Requisition {req.reference} has lines that name no product ("
            + ", ".join(loose) + "), so it could be approved but never ordered. Remove "
            "them and add the products instead, then submit it.")
    else:
        total = _requisition_total(req)
        req.status = "PENDING_APPROVAL"
        req.submitted_by = request.user
        req.save(update_fields=["status", "submitted_by", "updated_at"])
        messages.success(request, f"Requisition {req.reference} submitted for approval "
                                  f"(estimated {money(total)}).")
    return redirect("procurement:requisition_detail", pk=pk)


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


@login_required
@require_POST
def po_line_add(request, pk):
    """Add one line to a purchase order.

    The header totals are whatever the lines say they are, so they are not
    typed in beside them — a bill must be able to trust the order it came from.

    Lines change only while the order is a draft. Once it has gone to the
    supplier, the order is what the supplier was asked for: the goods receipt
    measures what arrived against it and the bill what is owed. A line added
    to an order already received in full re-opened it with nothing coming to
    close it, and a line added after sending was a promise the supplier never
    saw. Anything more is ordered on a new purchase order.
    """
    from apps.inventory.models import Product

    po = get_object_or_404(PurchaseOrder, pk=pk, company=request.active_company)
    if po.status != "DRAFT":
        messages.error(
            request,
            f"PO {po.reference} {po.state_phrase}, so its lines can no longer change — "
            "only a draft order can be edited. Raise a new purchase order for anything more.")
        return redirect("procurement:po_detail", pk=pk)

    product = pick(Product.objects.filter(company=po.company, is_active=True),
                   request.POST.get("product"))
    if not product:
        messages.error(request, "Choose a product from this company.")
        return redirect("procurement:po_detail", pk=pk)

    try:
        quantity = typed_number(request.POST.get("quantity"), "Quantity", QUANTITY)
        unit_cost = typed_number(request.POST.get("unit_cost"), "Unit cost", UNIT_COST,
                                 allow_zero=True)
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect("procurement:po_detail", pk=pk)
    if quantity is None:
        messages.error(request, "Enter the quantity ordered, a number greater than zero.")
        return redirect("procurement:po_detail", pk=pk)
    if unit_cost is None:
        messages.error(request, "Enter the unit cost agreed with the supplier (0 or more).")
        return redirect("procurement:po_detail", pk=pk)
    if not fits(quantity * unit_cost * Decimal("1.5"), MONEY):
        # Leaves room for the tax on top: the order's totals are money columns.
        messages.error(
            request,
            f"{quantity} × {unit_cost} is more than an order line can hold. "
            "Check the quantity and the unit cost.")
        return redirect("procurement:po_detail", pk=pk)

    from apps.accounting.models import TaxRate
    raw_tax = request.POST.get("tax_rate") or ""
    tax_rate = None
    if raw_tax:
        tax_rate = pick(TaxRate.objects.filter(company=po.company), raw_tax)
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
    messages.success(request, f"{product.name} added to {po.reference} — order total "
                              f"{money(po.total_amount)} {po.currency}.")
    return redirect("procurement:po_detail", pk=pk)


@login_required
@require_POST
def po_line_delete(request, pk, line_pk):
    """Take a line off a purchase order — while it is still a draft.

    Like adding one (po_line_add), removing a line once the order has gone to
    the supplier would make the order say something other than what was asked
    for and what the receipt and the bill are measured against.
    """
    po = get_object_or_404(PurchaseOrder, pk=pk, company=request.active_company)
    line = get_object_or_404(PurchaseOrderLine, pk=line_pk, order=po)
    if po.status != "DRAFT":
        messages.error(
            request,
            f"PO {po.reference} {po.state_phrase}, so its lines can no longer change — "
            "only a draft order can be edited. Cancel the order (if nothing has arrived "
            "on it) or close it short instead.")
    elif line.received_quantity > 0 or line.billed_quantity > 0:
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
    from apps.inventory.models import StockMovement
    from apps.organizations.models import Warehouse

    po = get_object_or_404(
        PurchaseOrder.objects.filter(company=request.active_company).select_related("warehouse"), pk=pk)
    if po.status not in ("SENT", "PARTIALLY_RECEIVED", "FULLY_RECEIVED"):
        hint = {
            "DRAFT": "Send it to the supplier before receiving anything against it.",
            "CLOSED": "Nothing more is expected on it. Raise a new order for anything "
                      "that still arrives.",
            "CANCELLED": "Nothing can be received against it. Raise a new order for "
                         "anything that arrives.",
            "BILLED": "Everything on it has arrived and been billed already.",
        }.get(po.status, "")
        messages.error(request, f"PO {po.reference} {po.state_phrase}. {hint}".strip())
        return redirect("procurement:po_detail", pk=pk)

    warehouse = po.warehouse
    raw_wh = request.POST.get("warehouse") or ""
    if raw_wh:
        warehouse = pick(Warehouse.objects.filter(company=po.company), raw_wh)
        if not warehouse:
            messages.error(request, "That warehouse does not belong to this company.")
            return redirect("procurement:grn", pk=pk)
    if not warehouse:
        active = Warehouse.objects.filter(company=po.company, is_active=True)
        warehouse = active.filter(is_default=True).first() or active.first()
    if not warehouse:
        messages.error(request, "This company has no active warehouse to receive goods "
                                "into. Add one, or reactivate one, under Organization → "
                                "Warehouses.")
        return redirect("procurement:grn", pk=pk)
    if not warehouse.is_active:
        # A retired warehouse is outside every count and every pick list:
        # goods received there would be in the book and nowhere anyone looks.
        messages.error(
            request,
            f"{warehouse.name} is inactive, so goods cannot be received into it. Choose "
            "an active warehouse on the receipt, or reactivate this one under "
            "Organization → Warehouses first.")
        return redirect("procurement:grn", pk=pk)

    wanted = []
    for line in po.lines.select_related("product"):
        try:
            qty = typed_number(request.POST.get(f"receive_qty_{line.pk}"),
                               f"Quantity received of {line.product.name}", QUANTITY,
                               allow_zero=True)
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
            return redirect("procurement:grn", pk=pk)
        if qty is None:
            continue  # an empty box means nothing arrived for that line
        if qty > line.to_receive:
            messages.error(
                request,
                f"Cannot receive {show_qty(qty)} of {line.product.name}: only "
                f"{show_qty(line.to_receive)} "
                "of this line is still outstanding.")
            return redirect("procurement:grn", pk=pk)
        if qty > 0:
            wanted.append((line, qty))
    if not wanted:
        messages.error(request, "Enter at least one quantity to receive.")
        return redirect("procurement:grn", pk=pk)

    today = datetime.date.today()
    try:
        with transaction.atomic():
            for line, qty in wanted:
                if line.product.track_inventory:
                    StockMovement.objects.create(
                        product=line.product, warehouse=warehouse, movement_type="RECEIPT",
                        quantity=qty, unit_cost=line.base_unit_cost,
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
    from apps.accounting.currency import book_currency, stamp_document, to_base
    from apps.core.models import SequenceCounter

    po = get_object_or_404(
        PurchaseOrder.objects.filter(company=request.active_company).select_related("supplier"), pk=pk)
    if po.status == "BILLED":
        messages.error(
            request,
            f"Nothing left to bill on {po.reference}: everything on it has been "
            "received and billed already.")
        return redirect("procurement:po_detail", pk=pk)
    if po.status not in ("SENT", "PARTIALLY_RECEIVED", "FULLY_RECEIVED"):
        messages.error(
            request,
            f"PO {po.reference} is {po.get_status_display()}: "
            + ("it is finished, and everything that arrived on it has been billed."
               if po.status == "CLOSED" else
               "it cannot be billed before it is sent and goods have arrived."))
        return redirect("procurement:po_detail", pk=pk)

    today = datetime.date.today()
    with transaction.atomic():
        # The order is locked while its unbilled quantities are read and taken:
        # a second click on "Bill" waits here, then finds nothing left to bill.
        po = PurchaseOrder.objects.select_for_update().select_related("supplier").get(pk=po.pk)
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

        # Rounded to the cent here, once, so the total on the bill is exactly
        # its subtotal plus its tax — the bill's columns hold cents and nothing finer.
        subtotal = sum((l.unit_cost * q for l, q in outstanding), Decimal("0")).quantize(
            Decimal("0.01"))
        tax = sum((l.unit_cost * q * ((l.tax_rate.rate if l.tax_rate else Decimal("0")) / 100)
                   for l, q in outstanding), Decimal("0")).quantize(Decimal("0.01"))
        # What the receipt put on the accrual for exactly these quantities,
        # worked out the way the receipt costed them: the order's price in book
        # money, per unit, times what arrived. Posting the bill clears this much
        # and no more, whatever the supplier's invoice finally says.
        accrual = sum((to_base(q * l.base_unit_cost, Decimal("1"))
                       for l, q in outstanding if l.product.track_inventory), Decimal("0"))
        # Services and other untracked lines were never received onto a shelf,
        # so nothing was accrued for them: the bill charges them straight to
        # the account the product is expensed through (or general expenses),
        # in the bill's own money, one row per account.
        from apps.accounting.gl_service import direct_charge_account
        charges = {}
        for l, q in outstanding:
            if l.product.track_inventory:
                continue
            account = direct_charge_account(po.company, l.product)
            row = charges.setdefault(account.pk, [Decimal("0"), []])
            row[0] += l.unit_cost * q
            row[1].append(l.product.name)
        direct = [{"account": str(pk), "amount": str(amount.quantize(Decimal("0.01"))),
                   "description": f"{', '.join(names)} — {po.reference}"}
                  for pk, (amount, names) in charges.items()
                  if amount.quantize(Decimal("0.01"))]

        reference = SequenceCounter.next_number(po.company, "BILL-")
        while Bill.objects.filter(company=po.company, reference__iexact=reference).exists():
            reference = SequenceCounter.next_number(po.company, "BILL-")
        bill = Bill(
            company=po.company, supplier=po.supplier, purchase_order=po,
            reference=reference,
            bill_date=today, due_date=today + datetime.timedelta(days=30),
            currency=po.currency, exchange_rate=po.exchange_rate,
            subtotal=subtotal, tax_amount=tax, total_amount=subtotal + tax,
            accrual_to_clear=accrual, direct_charges=direct,
            # Exactly which quantities this bill takes off the order, so that
            # cancelling it can hand back its own part and nobody else's.
            billed_lines=[{"line": l.pk, "quantity": str(q)} for l, q in outstanding],
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
        if all(l.to_bill <= 0 for l in po.lines.all()):
            # The last of what arrived is on this bill, so it answers whatever
            # the order still has standing on the accrual — to the cent, after
            # the drafts already raised take theirs. Rounding each receipt and
            # each bill separately cannot then leave a cent behind on an order
            # that is finished.
            from apps.accounting.gl_service import accrual_remaining
            pending = sum((b.accrual_to_clear for b in po.bills.filter(
                status="DRAFT", journal_entry__isnull=True, original_bill__isnull=True,
            ).exclude(pk=bill.pk)), Decimal("0"))
            standing = accrual_remaining(po) - pending
            if standing > 0 and standing != bill.accrual_to_clear:
                bill.accrual_to_clear = standing
                bill.save(update_fields=["accrual_to_clear"])
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
        f"Bill {bill.reference} for {money(bill.total_amount)} {bill.currency} raised from "
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

    try:
        amount = typed_number(request.POST.get("amount"), "Debit note amount", MONEY)
    except ValidationError as exc:
        messages.error(request, f"{exc.messages[0]} Enter the amount to debit, in the "
                                "bill's own currency.")
        return redirect("procurement:bill_detail", pk=pk)
    if amount is None:
        messages.error(request, "Enter the amount to debit, in the bill's own currency.")
        return redirect("procurement:bill_detail", pk=pk)
    if amount > bill.outstanding_amount:
        messages.error(
            request,
            f"Debit ({money(amount)} {bill.currency}) is more than {bill.reference} still "
            f"owes ({money(bill.outstanding_amount)} {bill.currency}). Ask the supplier for "
            "a refund instead.")
        return redirect("procurement:bill_detail", pk=pk)

    # A header-level document has no lines to re-price, so the tax is taken at
    # the same proportion the bill charged — the VAT input credited then matches
    # the VAT input debited, at whatever rate was in force on the bill.
    tax = (amount * bill.tax_amount / bill.total_amount).quantize(Decimal("0.01")) \
        if bill.total_amount else Decimal("0")
    tax = min(tax, amount)
    today = datetime.date.today()

    with transaction.atomic():
        # Re-read under a lock: the note's book value is worked out from the
        # bill's balance, which a payment in another tab must not move halfway.
        bill = Bill.objects.select_for_update().select_related("supplier").get(pk=bill.pk)
        if amount > bill.outstanding_amount:
            messages.error(
                request,
                f"{bill.reference} now owes {money(bill.outstanding_amount)} {bill.currency}, "
                f"so a debit of {money(amount)} cannot be raised. Nothing was recorded.")
            return redirect("procurement:bill_detail", pk=pk)
        note = Bill(
            company=bill.company, supplier=bill.supplier,
            purchase_order_id=bill.purchase_order_id, original_bill=bill,
            reference=SequenceCounter.next_number(bill.company, "DN-"),
            bill_date=today, due_date=today,
            currency=bill.currency, exchange_rate=bill.exchange_rate,
            subtotal=amount - tax, tax_amount=tax, total_amount=amount,
            status="DEBIT_NOTE",
            notes=request.POST.get("reason", "").strip() or f"Debit note against {bill.reference}",
            # The note is a part of the bill, so it is worth the bill's balance
            # in book money before it less the balance after it — the parts of
            # a foreign bill then add up to its whole, and AP closes to nothing.
            total_amount_base=bill.settlement_base(amount),
        )
        if not note.exchange_rate and note.currency == bill.currency:
            note.exchange_rate = bill.rate
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
        f"Debit note {note.reference} for {money(amount)} {bill.currency} posted against "
        f"{bill.reference}.")
    return redirect("procurement:bill_detail", pk=bill.pk)


# ------------------------- AP actions -------------------------

class BillUpdateView(LoginRequiredMixin, UpdateView):
    """Edit a bill while it is still a draft the ledger has not seen.

    Once a bill is posted its figures are in the general ledger, and payments
    and debit notes are measured against them. Changing the paper after that
    leaves the bill, the payable it raised and the VAT it claimed telling three
    different stories — a part-paid bill cut below what was already paid shows
    a negative balance while the ledger still owes the old total. A posted bill
    is put right the way the ledger is: a debit note for the part that is
    wrong, or cancellation (which posts its own reversal) and a fresh bill.
    """
    model = Bill
    form_class = BillForm
    template_name = "procurement/bill_form.html"

    def get_queryset(self):
        return Bill.objects.filter(company=self.request.active_company)

    def dispatch(self, request, *args, **kwargs):
        obj = self.get_object()
        if obj.status != "DRAFT" or obj.journal_entry_id:
            messages.error(
                request,
                f"Bill {obj.reference} cannot be edited: it is "
                f"{obj.get_status_display().lower()}"
                + (" and already posted to the GL" if obj.journal_entry_id else "")
                + ". Only a draft can change. Raise a debit note for the part that is "
                "wrong, or cancel the bill and enter it again.")
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
        with transaction.atomic():
            # Re-checked under a lock: a draft posted or paid in another tab
            # while this form was open is no longer a draft to overwrite.
            current = Bill.objects.select_for_update().get(pk=self.object.pk)
            if current.status != "DRAFT" or current.journal_entry_id:
                messages.error(
                    self.request,
                    f"Bill {current.reference} was posted while you were editing it, so "
                    "the changes were not saved. Raise a debit note instead.")
                return redirect("procurement:bill_detail", pk=current.pk)
            response = super().form_valid(form)
        messages.success(self.request, f"Bill {form.instance.reference} updated.")
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
            f"Cannot cancel {bill.reference}: {money(bill.paid_amount)} paid and "
            f"{money(bill.credited_amount)} debited against it. Reverse the payment or "
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

    "Bill this order" records on each bill exactly which order lines it took
    and how much of each (Bill.billed_lines), so cancelling one bill of several
    hands back its own quantities and nobody else's. The order can then be
    billed again for them, and the accrual its receipt put up — which the
    cancellation's reversing entry has stood back up on 2110, or which a draft
    never cleared — is answered by that new bill instead of being stranded.

    A bill raised before the quantities were recorded is a header-level
    document with no per-line figures, so it can only be attributed back when
    it is the sole bill standing against the order; with another live, which
    line it paid for would be a guess, and a guessed quantity on an order is
    how goods get billed twice. A debit note takes no quantities at all.
    """
    po = bill.purchase_order
    if not po or bill.original_bill_id:
        return
    if bill.billed_lines:
        lines = {l.pk: l for l in po.lines.select_for_update()}
        for row in bill.billed_lines:
            line = lines.get(row.get("line"))
            if line is None:
                continue
            taken = Decimal(str(row.get("quantity") or "0"))
            line.billed_quantity = max(
                (line.billed_quantity or Decimal("0")) - taken, Decimal("0"))
            line.save(update_fields=["billed_quantity"])
    elif po.bills.exclude(pk=bill.pk).exclude(status="CANCELLED").filter(
            original_bill__isnull=True).exists():
        messages.info(
            request,
            f"{po.reference} still has another bill against it, so its billed "
            "quantities were left as they are rather than guessed at.",
        )
        return
    else:
        for line in po.lines.all():
            line.billed_quantity = Decimal("0")
            line.save(update_fields=["billed_quantity"])
    if po.status in ("BILLED", "CLOSED"):
        # The order is no longer finished (billed, or closed short with
        # everything that arrived billed): say what the quantities actually are,
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
    elif bill.status != "DRAFT":
        # A free-goods bill settles its order with no journal at all (there is
        # no money to book), so it is posted without an entry to point at.
        messages.info(request, f"Bill {bill.reference} is already posted.")
    else:
        if _post_bill_to_gl(request, bill, request.user):
            if bill.journal_entry_id:
                messages.success(request, f"Bill '{bill.reference}' posted to GL.")
            else:
                messages.success(
                    request,
                    f"Bill '{bill.reference}' is for nothing — the goods on it were free — "
                    "so it settles the order with no journal entry and nothing to pay.")
    return redirect("procurement:bill_detail", pk=pk)


# A draft is posted on the way to being paid; anything else is either settled
# already or not a payable at all — a cancelled bill, or a debit note.
PAYABLE_BILL_STATUSES = ("DRAFT", "RECEIVED", "PARTIAL", "OVERDUE")


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
    if bill.status not in PAYABLE_BILL_STATUSES:
        messages.error(
            request,
            f"Cannot pay {bill.reference}: it is {bill.get_status_display().lower()}. "
            "Only a bill the supplier is still owed can take a payment.")
        return redirect("procurement:bill_detail", pk=pk)
    # Read the way every other money box is read (apps/inventory/numbers.py):
    # cents and nothing finer, no exponents, no thousands separators. Rounding
    # 100.005 to the nearest even cent, or reading "1,250" as 1250, pays the
    # supplier a figure nobody typed.
    try:
        amount = typed_number(request.POST.get("amount"), "Payment amount", MONEY)
    except ValidationError as exc:
        messages.error(request, f"{exc.messages[0]} Enter the amount paid, in "
                                f"{bill.currency}, to the cent.")
        return redirect("procurement:bill_detail", pk=pk)
    if amount is None:
        messages.error(request, f"Enter the amount paid, in {bill.currency}, a number "
                                "greater than zero.")
        return redirect("procurement:bill_detail", pk=pk)
    if amount > bill.outstanding_amount:
        messages.error(request, f"Payment ({money(amount)} {bill.currency}) exceeds outstanding "
                                f"({money(bill.outstanding_amount)} {bill.currency}).")
        return redirect("procurement:bill_detail", pk=pk)

    company = request.active_company
    ap = Account.objects.filter(company=company, account_type="PAYABLE", is_active=True).order_by("code").first()

    # The cash side is chosen, not guessed: money leaves a specific account.
    # A request that names none is paid from the company's main bank account
    # — never from whichever cash account happens to have the lowest code,
    # which is the till (1100): a supplier is not paid out of the shop drawer
    # unless somebody chose that on purpose.
    bank = None
    raw_bank = request.POST.get("gl_account") or ""
    if raw_bank:
        bank = pick(Account.objects.filter(
            company=company, account_type__in=["BANK", "CASH"], is_active=True), raw_bank)
        if not bank:
            messages.error(request, "That payment account does not belong to this company.")
            return redirect("procurement:bill_detail", pk=pk)
    else:
        bank = main_bank_account(company)
        if bank is None:
            messages.error(
                request,
                "Choose the account the payment was made from under “Paid from”. This "
                "company has no bank account in its chart, so none is assumed — paying "
                "from a cash account has to be chosen on purpose.")
            return redirect("procurement:bill_detail", pk=pk)
    if not ap:
        messages.error(request, "The chart of accounts has no Accounts Payable account "
                                "(2100). Add one before paying suppliers.")
        return redirect("procurement:bill_detail", pk=pk)

    today = datetime.date.today()
    payment_date = today
    raw_date = request.POST.get("payment_date") or ""
    if raw_date:
        try:
            payment_date = datetime.datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            messages.error(request, "Payment date must be a real date (YYYY-MM-DD).")
            return redirect("procurement:bill_detail", pk=pk)
    # Money cannot leave before the bill it pays was issued, nor on a day that
    # has not happened yet: a payment is a record of what the bank did.
    if payment_date < bill.bill_date:
        messages.error(
            request,
            f"The payment date ({payment_date:%d %b %Y}) is before {bill.reference} was "
            f"issued ({bill.bill_date:%d %b %Y}). Enter the day the money actually left.")
        return redirect("procurement:bill_detail", pk=pk)
    if payment_date > today:
        messages.error(
            request,
            f"The payment date ({payment_date:%d %b %Y}) is in the future. Record the "
            "payment on the day the money leaves the account.")
        return redirect("procurement:bill_detail", pk=pk)

    # Payables leave the book at the value the bill was raised at; the cash
    # goes out at the payment day's rate, and the gap between them is real money.
    from apps.accounting.currency import document_rate
    from apps.accounting.gl_service import _book_exchange_difference
    from apps.accounting.models import ExchangeRate, to_base

    try:
        bill_rate = document_rate(bill, bill.bill_date)
        payment_rate = ExchangeRate.rate_for(company, bill.currency, payment_date)
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect("procurement:bill_detail", pk=pk)
    bank_base = to_base(amount, payment_rate)

    # Posting a draft bill and paying it are one event. Both happen inside a
    # single transaction, so a payment the ledger refuses — dated in a closed
    # month, say — also takes back the bill's own posting: the bookkeeper is
    # left with the draft exactly as it was, not a bill half on the ledger
    # with no payment against it. The refusal is shown on the screen, in the
    # ledger's own words, instead of failing the page.
    posted_first = False
    was_draft = not bill.journal_entry_id
    try:
        with transaction.atomic():
            if not bill.journal_entry_id:
                # Paying an unposted bill would debit AP with no matching
                # credit. It is posted only once everything about the payment
                # itself has checked out, so a mistyped account or date does
                # not post a draft by accident.
                from apps.accounting.gl_service import post_bill
                try:
                    post_bill(bill, user=request.user)
                except ValidationError:
                    raise
                except Exception as exc:
                    raise ValidationError(
                        f"{bill.reference} could not be posted to the GL: {exc}")
                posted_first = True
            # Re-read the bill under a lock: two payments sent together must
            # not both pass the outstanding check and pay the supplier twice.
            bill = Bill.objects.select_for_update().get(pk=bill.pk)
            if bill.status not in PAYABLE_BILL_STATUSES or amount > bill.outstanding_amount:
                raise ValidationError(
                    f"{bill.reference} is {bill.get_status_display().lower()} with "
                    f"{money(bill.outstanding_amount)} outstanding, so {money(amount)} cannot "
                    "be paid against it.")
            # The payable comes off at its share of the bill's own book value:
            # the balance before this payment less the balance after it, both
            # at the bill's rate. Converting the payment on its own would, over
            # a few part payments, leave a cent on AP for a bill paid in full.
            ap_base = bill.settlement_base(amount)
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
            bill.outstanding_amount = bill.total_amount - bill.paid_amount - bill.credited_amount
            if bill.outstanding_amount <= 0:
                bill.status = "PAID"
            else:
                bill.status = "PARTIAL"
            bill.save(update_fields=["paid_amount", "outstanding_amount", "status"])
    except ValidationError as exc:
        messages.error(
            request,
            f"Could not pay {bill.reference}: {' '.join(exc.messages)} Nothing was recorded"
            + (f" and {bill.reference} is still a draft." if was_draft else "."))
        return redirect("procurement:bill_detail", pk=pk)
    if posted_first:
        messages.info(request, f"Bill {bill.reference} was posted to the GL first.")
    messages.success(request, f"Payment {money(amount)} {bill.currency} recorded for "
                              f"{bill.reference}. GL entry {je.reference} posted.")
    if je.calendar_warning:
        # Allowed, but said out loud: a payment dated into a closed period or
        # outside any period needs a second look.
        messages.warning(request, je.calendar_warning)
    return redirect("procurement:bill_detail", pk=pk)


def main_bank_account(company):
    """The account a supplier is paid from when the form names none.

    The company's main bank account: the lowest-coded active BANK account
    (1200 in the standard chart). Never a CASH account — the lowest-coded of
    those is the till float (1100) — so with no bank account at all there is
    no default, and the person paying has to choose.
    """
    from apps.accounting.models import Account
    return Account.objects.filter(
        company=company, account_type="BANK", is_active=True).order_by("code").first()


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
    """Call off an order nothing has happened on yet.

    Once goods have arrived the order cannot be cancelled: they are on the
    shelf and on the accrual, and a cancelled order can no longer be billed,
    so that accrual would never clear. An order part-delivered whose rest will
    never come is closed short instead.
    """
    po = get_object_or_404(PurchaseOrder, pk=pk, company=request.active_company)
    if po.status in ("BILLED", "FULLY_RECEIVED", "CLOSED", "CANCELLED"):
        messages.error(request, f"PO {po.reference} is {po.get_status_display().lower()}, "
                                "so it cannot be cancelled.")
    elif po.bills.exclude(status="CANCELLED").exists():
        messages.error(
            request,
            f"Cannot cancel {po.reference}: bills have been raised against it. "
            "Cancel those first — they are the money side of the order.")
    elif po.lines.filter(received_quantity__gt=0).exists():
        messages.error(
            request,
            f"Cannot cancel {po.reference}: goods have already been received against "
            "it. Bill what arrived with \"Bill this order\", then use \"Close short\" "
            "if the rest of the order is never coming.")
    else:
        with transaction.atomic():
            po.status = "CANCELLED"
            po.save(update_fields=["status", "updated_at"])
            # The need the order answered has not gone away because the order
            # did: the requisition goes back to approved, so it can be ordered
            # again (from another supplier, say) or cancelled, instead of
            # sitting on "PO created" with no live order behind it.
            req = po.requisition
            reopened = False
            if (req is not None and req.status == "PO_CREATED"
                    and not req.purchaseorder_set.exclude(status="CANCELLED").exists()):
                req.status = "APPROVED"
                req.save(update_fields=["status", "updated_at"])
                reopened = True
        messages.success(
            request, f"PO {po.reference} cancelled."
            + (f" Requisition {req.reference} is approved again and can be ordered anew."
               if reopened else ""))
    return redirect("procurement:po_detail", pk=pk)


@login_required
@require_POST
def po_close_short(request, pk):
    """Finish a part-delivered order whose remaining goods will never come.

    The order is closed as it stands: what arrived stays received and what was
    billed stays billed — nothing is reversed, because none of it was wrong.
    What is refused is closing an order the ledger is still waiting on. Goods
    that arrived and have not been billed (or whose bill is still a draft) sit
    on the received-not-invoiced accrual, and a closed order can no longer be
    billed, so that accrual would never clear. Bill what arrived first, post
    the bill, then close the order short.
    """
    from apps.accounting.gl_service import accrual_remaining

    po = get_object_or_404(PurchaseOrder, pk=pk, company=request.active_company)
    back = redirect("procurement:po_detail", pk=pk)
    if po.status != "PARTIALLY_RECEIVED":
        hint = {
            "DRAFT": " Nothing has been received on it, so cancel it instead.",
            "SENT": " Nothing has been received on it, so cancel it instead.",
            "FULLY_RECEIVED": " Everything ordered has arrived; bill it instead.",
        }.get(po.status, "")
        messages.error(
            request,
            f"PO {po.reference} is {po.get_status_display().lower()}: only an order that "
            f"is part received can be closed short.{hint}")
        return back
    with transaction.atomic():
        po = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
        unbilled = [l for l in po.lines.select_related("product") if l.to_bill > 0]
        if unbilled:
            names = ", ".join(f"{show_qty(l.to_bill)} × {l.product.name}" for l in unbilled)
            messages.error(
                request,
                f"Cannot close {po.reference} short: {names} arrived and has not been "
                "billed yet. Bill what arrived with \"Bill this order\" first.")
            return back
        drafts = po.bills.filter(status="DRAFT", original_bill__isnull=True)
        standing = accrual_remaining(po)
        if drafts.exists() or standing:
            messages.error(
                request,
                f"Cannot close {po.reference} short: "
                + (f"bill {drafts.first().reference} is still a draft. Post it first"
                   if drafts.exists() else
                   f"{money(standing)} is still standing on the goods-received accrual for "
                   "it. Post the bill for what arrived first")
                + ", so the accrual is cleared before the order is closed.")
            return back
        po.status = "CLOSED"
        po.save(update_fields=["status", "updated_at"])
    short = sum((l.to_receive for l in po.lines.all()), Decimal("0"))
    messages.success(
        request,
        f"PO {po.reference} closed short. {show_qty(short)} unit(s) that never arrived "
        "are no longer expected; what was received and billed stays as it is.")
    return back


@login_required
@require_POST
def requisition_approve(request, pk):
    """Agree to a submitted requisition: PENDING_APPROVAL -> APPROVED.

    Approval is a second person's check on spending the company's money, so
    it is refused to the person the requisition was raised for and to the
    person who submitted it — whatever their role. A finance manager who
    needs something buys it through somebody else's approval like anyone.
    """
    req = _requisition_for(request, pk)
    user = request.user
    own = user.pk in {req.requested_by_id, req.submitted_by_id}
    if req.status != "PENDING_APPROVAL":
        messages.error(
            request,
            f"Requisition {req.reference} is {req.state_phrase}: only a "
            "requisition submitted for approval can be approved."
            + (" Submit it first." if req.status == "DRAFT" else ""))
    elif own:
        messages.error(
            request,
            f"You cannot approve requisition {req.reference}: you "
            + ("raised" if user.pk == req.requested_by_id else "submitted")
            + " it, and nobody approves their own request to spend. Another approver "
            "has to decide it.")
    else:
        from django.utils import timezone
        req.status = "APPROVED"
        req.approved_by = request.user
        req.approved_at = timezone.now()
        req.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
        messages.success(request, f"Requisition {req.reference} approved.")
    return redirect("procurement:requisition_detail", pk=pk)


@login_required
@require_POST
def requisition_reject(request, pk):
    """Turn a submitted requisition down.

    Like approval, it answers a request that is waiting for an answer: an
    approved requisition may already be on order, a draft has not been asked
    yet, and a rejected or ordered one has had its answer.
    """
    req = _requisition_for(request, pk)
    if req.status != "PENDING_APPROVAL":
        messages.error(
            request,
            f"Requisition {req.reference} is {req.state_phrase}: only a "
            "requisition waiting for approval can be rejected.")
    else:
        req.status = "REJECTED"
        req.save(update_fields=["status", "updated_at"])
        messages.success(request, f"Requisition {req.reference} rejected.")
    return redirect("procurement:requisition_detail", pk=pk)


@login_required
@require_POST
def requisition_cancel(request, pk):
    """Withdraw a requisition that is no longer needed: -> CANCELLED.

    A draft, one waiting for approval, or one approved and not yet ordered can
    be called off. One already on a purchase order is cancelled by cancelling
    the order (which hands it back as approved); a rejected one has had its
    answer already.
    """
    req = _requisition_for(request, pk)
    if req.status not in ("DRAFT", "PENDING_APPROVAL", "APPROVED"):
        hint = (" Cancel its purchase order first; that hands it back as approved."
                if req.status == "PO_CREATED" else "")
        messages.error(
            request,
            f"Requisition {req.reference} is {req.state_phrase}, so it cannot be "
            f"cancelled.{hint}")
    else:
        req.status = "CANCELLED"
        req.save(update_fields=["status", "updated_at"])
        messages.success(request, f"Requisition {req.reference} cancelled.")
    return redirect("procurement:requisition_detail", pk=pk)


@login_required
@require_POST
def requisition_to_po(request, pk):
    """Raise a draft purchase order from an approved requisition.

    The order is placed with the supplier chosen here, for the requisition's
    products and quantities at their estimated costs; the buyer then corrects
    the prices to what the supplier quoted and sends it. A described item with
    no product cannot go on an order — an order line is a product the goods
    receipt can put on a shelf — so every line has to name one first.
    """
    from apps.accounting.currency import book_currency, stamp_document
    from apps.core.models import SequenceCounter
    from apps.crm.models import Supplier

    req = _requisition_for(request, pk)
    back = redirect("procurement:requisition_detail", pk=pk)
    if req.status != "APPROVED":
        messages.error(
            request,
            f"Requisition {req.reference} is {req.state_phrase}: an order "
            "is raised only from an approved requisition.")
        return back
    supplier = pick(Supplier.objects.filter(company=req.company, is_active=True),
                    request.POST.get("supplier"))
    if not supplier:
        messages.error(request, "Choose the supplier the order is to be placed with.")
        return back
    lines = list(req.lines.select_related("product", "product__tax_rate"))
    loose = [l.description for l in lines if l.product_id is None]
    if loose:
        messages.error(
            request,
            "These lines name no product, so they cannot go on a purchase order: "
            + ", ".join(loose) + ". Order them by hand, or enter them as products "
            "on a new requisition.")
        return back

    with transaction.atomic():
        # Locked so a second click finds the requisition already ordered
        # instead of raising a second order for the same need.
        req = PurchaseRequisition.objects.select_for_update().get(pk=req.pk)
        if req.status != "APPROVED":
            messages.error(request, f"Requisition {req.reference} has already been ordered.")
            return back
        warehouse = next((l.warehouse for l in lines if l.warehouse_id), None)
        # Placed in the money the supplier is paid in, at the day's rate, as an
        # order typed on the order form is. A dollar supplier's order raised in
        # shillings would receive the goods at the dollar price read as
        # shillings, and the bill raised from it would ask to pay that.
        today = datetime.date.today()
        po = PurchaseOrder(
            company=req.company, supplier=supplier, requisition=req,
            order_date=today, expected_delivery_date=req.required_date,
            warehouse=warehouse,
            currency=(supplier.currency or book_currency(req.company)).upper(),
            notes=f"Raised from requisition {req.reference}",
        )
        try:
            stamp_document(po, today)
        except ValidationError as exc:
            messages.error(
                request,
                f"{supplier.name} is paid in {po.currency}: {exc.messages[0]} File the "
                f"day's {po.currency} rate first, then raise the order.")
            return back
        po.reference = SequenceCounter.next_number(req.company, "PO-")
        po.save()
        for line in lines:
            PurchaseOrderLine.objects.create(
                order=po, product=line.product, description=line.description,
                quantity=line.quantity, unit_cost=line.estimated_unit_cost,
                tax_rate=line.product.tax_rate,
            )
        po.calculate_totals()
        req.status = "PO_CREATED"
        req.save(update_fields=["status", "updated_at"])
    messages.success(
        request,
        f"Purchase order {po.reference} raised from {req.reference} for {supplier.name} "
        f"({money(po.total_amount)} {po.currency}). It is a draft: check the prices "
        "against the supplier's quote, then send it.")
    return redirect("procurement:po_detail", pk=po.pk)
