import json
from decimal import Decimal

from django import forms
from apps.core.forms import date_input, is_new, pick_currency
from apps.inventory.numbers import PlainNumbersMixin, money
from .models import Bill, PurchaseOrder, PurchaseRequisition


class PurchaseRequisitionForm(PlainNumbersMixin, forms.ModelForm):
    """The header of a request to buy: who wants it, by when, for which team.

    The lines — what, how many, roughly what it will cost — are added on the
    requisition itself once it is saved, and it is submitted for approval from
    there.
    """

    class Meta:
        model = PurchaseRequisition
        fields = ["reference", "department", "required_date", "notes"]
        widgets = {
            "required_date": date_input(),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company

    def clean_reference(self):
        # Two requisitions under one reference cannot both be approved, raised
        # into orders and traced back: the reference is how the order, the
        # approver and the person who asked find each other.
        reference = (self.cleaned_data.get("reference") or "").strip()
        if not reference:
            raise forms.ValidationError("Give the requisition a reference.")
        # A number from the company's own PR- series is the preview the form
        # showed: if somebody saved with it meanwhile, the save takes the next
        # one (SequenceCounter.claim_number). Only a hand-typed one can clash.
        series = reference[3:] if reference.upper().startswith("PR-") else ""
        if is_new(self) and series.isdigit():
            return reference
        if self.company is not None:
            twin = PurchaseRequisition.objects.filter(
                company=self.company, reference__iexact=reference).exclude(
                pk=self.instance.pk).first()
            if twin:
                raise forms.ValidationError(
                    f"Requisition {twin.reference} already exists "
                    f"({twin.get_status_display().lower()}). Use another reference, "
                    "or open that requisition instead.")
        return reference


class PurchaseOrderForm(PlainNumbersMixin, forms.ModelForm):
    """A purchase order's header, in the money the supplier is paid in.

    The currency is chosen the way a bill's is, from the currencies the book
    holds a rate for, and the day's rate is stamped on the order when it is
    saved. The goods receipt costs stock at the order price times that rate,
    and the bill raised from the order inherits both, so an order placed in
    dollars stays in dollars from the order to the payment — and the stock it
    brings in is valued in shillings at the rate the order was placed at.
    """

    class Meta:
        model = PurchaseOrder
        fields = ["reference", "supplier", "order_date", "expected_delivery_date",
                  "warehouse", "currency", "notes"]
        widgets = {
            "order_date": date_input(),
            "expected_delivery_date": date_input(),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        if not company:
            return
        from apps.crm.models import Supplier
        from apps.organizations.models import Warehouse
        self.fields["supplier"].queryset = Supplier.objects.filter(company=company, is_active=True)
        self.fields["warehouse"].queryset = Warehouse.objects.filter(company=company, is_active=True)
        pick_currency(self, company)
        # The currency follows the supplier unless somebody chose otherwise: the
        # select is pre-set to the supplier's money as soon as one is picked
        # (each supplier option carries its currency for the page to read),
        # and a form sent with no currency at all takes the supplier's.
        self.supplier_currencies = {
            str(s.pk): (s.currency or "").upper()
            for s in self.fields["supplier"].queryset.only("pk", "currency")}
        self.fields["supplier"].widget.attrs["data-currencies"] = json.dumps(
            self.supplier_currencies)
        self.fields["supplier"].widget.attrs["onchange"] = (
            "var c=JSON.parse(this.dataset.currencies||'{}')[this.value];"
            "var s=this.form.elements['currency'];"
            "if(c&&s&&[].some.call(s.options,function(o){return o.value===c;}))s.value=c;")
        if not self.is_bound and is_new(self):
            chosen = self.initial.get("supplier")
            if chosen and str(getattr(chosen, "pk", chosen)) in self.supplier_currencies:
                self.initial["currency"] = self.supplier_currencies[str(getattr(chosen, "pk", chosen))]

    def clean(self):
        cleaned = super().clean()
        supplier = cleaned.get("supplier")
        typed = (self.data.get(self.add_prefix("currency")) or "").strip()
        if supplier and is_new(self) and not typed and "currency" in self.fields:
            own = (supplier.currency or "").upper()
            if own and own in dict(self.fields["currency"].choices):
                cleaned["currency"] = own
        # The model's own clean() then stamps the rate for the currency chosen.
        return cleaned

    def clean_reference(self):
        reference = (self.cleaned_data.get("reference") or "").strip()
        # A number from the company's own PO- series is only the preview the form
        # showed: if somebody saved with it meanwhile, the save takes the next one
        # (SequenceCounter.claim_number). Only a reference typed by hand can clash.
        series = reference[3:] if reference.upper().startswith("PO-") else ""
        if is_new(self) and series.isdigit():
            return reference
        if reference and self.company is not None:
            twin = PurchaseOrder.objects.filter(
                company=self.company, reference__iexact=reference).exclude(
                pk=self.instance.pk).first()
            if twin:
                raise forms.ValidationError(
                    f"Purchase order {twin.reference} already exists. Use another reference.")
        return reference


def chargeable_accounts(company):
    """The accounts a bill with no order behind it may be charged to.

    Expenses, costs and assets — a service, a utility, a piece of equipment.
    The stock accounts are left out on purpose: stock is valued by what is on
    the shelf, and a bill alone puts nothing on the shelf, so a bill coded to
    inventory would raise the account above the stock it is meant to describe.
    So is Stock Received Not Invoiced (2110): only a goods receipt on a
    purchase order accrues there, and that order's own bill — raised with
    "Bill this order" — is what clears it. A typed bill coded to 2110 would
    debit an accrual no receipt ever credited.

    Nor may it reach an account the ledger keeps for a job of its own
    (gl_service.reserved_accounts): accumulated depreciation (1690) and any
    other contra account, which only the depreciation run writes; a header
    such as 1000 or 1600, whose balance is the sum of the accounts under it;
    the exchange revaluation accounts (1350), which the revaluation posts and
    reverses as a pair; and the control accounts (banks, receivables, payables,
    tax), each proved against a sub-ledger a typed bill knows nothing about.
    """
    from apps.accounting.gl_service import reserved_accounts
    from apps.accounting.models import Account
    from apps.inventory.models import Product

    stock = set(Product.objects.filter(company=company, inventory_account__isnull=False)
                .values_list("inventory_account_id", flat=True))
    return (Account.objects.filter(company=company, is_active=True)
            .filter(account_type__in=["EXPENSE", "COST_OF_GOODS", "ASSET"])
            .exclude(code__in=["1400", "1450"]).exclude(pk__in=stock)
            .exclude(pk__in=reserved_accounts(company))
            .order_by("code"))


class BillForm(PlainNumbersMixin, forms.ModelForm):
    """A supplier bill typed in from the paper.

    A new bill here is for something that was not ordered through a purchase
    order — a service, rent, a repair — and is charged to the account the
    bookkeeper picks. Goods bought on an order are billed from the order itself
    with "Bill this order": that is the screen that knows which quantities have
    arrived, which are already billed and what the receipt accrued for them.
    Letting a typed bill name an order as well would let the same goods be
    billed twice, and — when the bill came before the goods — put them into
    stock twice.
    """

    class Meta:
        model = Bill
        fields = [
            "reference", "supplier", "purchase_order", "charge_account", "supplier_reference",
            "bill_date", "due_date", "currency", "subtotal", "tax_amount",
            "total_amount", "notes",
        ]
        widgets = {
            "bill_date": date_input(),
            "due_date": date_input(),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        if not company:
            return
        from apps.crm.models import Supplier
        self.fields["supplier"].queryset = Supplier.objects.filter(company=company, is_active=True)
        accounts = chargeable_accounts(company)
        self.fields["charge_account"].queryset = accounts
        # No blank choice on screen: every bill typed in is charged somewhere
        # the bookkeeper chose, and never to stock by default.
        self.fields["charge_account"].empty_label = None
        pick_currency(self, company)
        if is_new(self):
            import datetime
            # The order link is not offered on a new bill at all (see the class
            # note); clean() refuses one that is posted anyway.
            del self.fields["purchase_order"]
            default = accounts.filter(code="6000").first() or accounts.filter(
                account_type="EXPENSE").first()
            if default:
                self.fields["charge_account"].initial = default.pk
            self.fields["bill_date"].initial = datetime.date.today()
            self.fields["due_date"].initial = datetime.date.today() + datetime.timedelta(days=30)
        else:
            # A draft raised from an order stays on that order: the quantities
            # it took are recorded there. It is charged through the accrual, so
            # it has no account of its own to choose.
            self.fields["purchase_order"].queryset = PurchaseOrder.objects.filter(company=company)
            self.fields["purchase_order"].empty_label = "— Not linked to an order"
            self.fields["purchase_order"].disabled = True
            if self.instance.purchase_order_id:
                self.fields["charge_account"].disabled = True
                self.fields["charge_account"].required = False
                # The bill answers the order in the order's money, at the
                # order's rate: that is what the receipt costed the goods at and
                # what the accrual holds. Re-labelling a shilling draft as
                # dollars would keep the shilling rate of one and value the
                # dollars as shillings.
                if "currency" in self.fields:
                    self.fields["currency"].disabled = True

    def clean_reference(self):
        # A bill's own reference is how a payment, a debit note and the
        # supplier statement find it; two bills under one reference cannot be
        # told apart on any of them.
        reference = (self.cleaned_data.get("reference") or "").strip()
        if reference and self.company is not None:
            twin = Bill.objects.filter(
                company=self.company, reference__iexact=reference).exclude(
                pk=self.instance.pk).first()
            if twin:
                raise forms.ValidationError(
                    f"Bill reference {twin.reference} is already used in this company "
                    f"({twin.supplier.name}, {twin.bill_date:%d %b %Y}). Use another "
                    "reference, or leave it blank to take the next number.")
        return reference

    def clean_bill_date(self):
        # A bill is a supplier's paper that has already arrived. One dated more
        # than a year ahead, or more than ten years back, is a typing slip in
        # the year — and it would land in a period nobody is looking at.
        import datetime
        bill_date = self.cleaned_data.get("bill_date")
        if bill_date is None:
            return bill_date
        today = datetime.date.today()
        if bill_date > today + datetime.timedelta(days=365):
            raise forms.ValidationError(
                f"The bill date {bill_date:%d %b %Y} is more than a year ahead. Enter the "
                "date printed on the supplier's invoice.")
        try:
            earliest = today.replace(year=today.year - 10)
        except ValueError:  # 29 February
            earliest = today.replace(year=today.year - 10, day=28)
        if bill_date < earliest:
            raise forms.ValidationError(
                f"The bill date {bill_date:%d %b %Y} is more than ten years ago. Enter the "
                "date printed on the supplier's invoice.")
        return bill_date

    def clean(self):
        cleaned = super().clean()
        currency = cleaned.get("currency")
        moved = (not is_new(self) and not self.instance.purchase_order_id
                 and cleaned.get("bill_date") and cleaned["bill_date"] != self.instance.bill_date)
        if moved or (not is_new(self) and currency
                     and currency != (self.instance.currency or "")):
            # A typed draft moved to another currency, or to another date, is
            # valued afresh at the rate for its currency on its date; the rate
            # booked for the old code or the old day says nothing about the
            # new one. (A draft from an order keeps the order's rate.)
            self.instance.exchange_rate = None
        if is_new(self) and (self.data.get(self.add_prefix("purchase_order")) or "").strip():
            raise forms.ValidationError(
                "A bill for goods on a purchase order is raised from the order: open the "
                "order and use \"Bill this order\". It bills only what has arrived and "
                "has not been billed yet, so the same goods cannot be paid for twice.")
        bill_date = cleaned.get("bill_date")
        due_date = cleaned.get("due_date")
        if bill_date and due_date and due_date < bill_date:
            self.add_error(
                "due_date",
                f"The due date ({due_date:%Y-%m-%d}) is before the bill date "
                f"({bill_date:%Y-%m-%d}). A bill cannot fall due before it was issued.")
        po = cleaned.get("purchase_order") if "purchase_order" in self.fields else None
        supplier = cleaned.get("supplier")
        if po and supplier and po.supplier_id != supplier.pk:
            self.add_error(
                "supplier",
                f"{po.reference} was placed with {po.supplier.name}, not {supplier.name}. "
                "A bill answers the order of the supplier who sent it.")
        if po and po.status == "CANCELLED":
            self.add_error(
                "purchase_order",
                f"{po.reference} is cancelled, so nothing on it can be billed.")
        if po or (not is_new(self) and self.instance.purchase_order_id):
            cleaned["charge_account"] = None
        elif not cleaned.get("charge_account") and self.company is not None:
            # A bill typed in with no account chosen is a general expense
            # (6000) — the bill records it, so the posting, the debit note and
            # the reader of the bill all see the same account. It is never
            # charged to stock by default: nothing a bill says puts goods on a
            # shelf.
            from apps.accounting.gl_service import unordered_charge_account
            cleaned["charge_account"] = unordered_charge_account(self.company)
            self.instance.charge_account = cleaned["charge_account"]
        subtotal = cleaned.get("subtotal") or Decimal("0")
        tax = cleaned.get("tax_amount") or Decimal("0")
        total = cleaned.get("total_amount")
        if total is None:
            return cleaned
        if subtotal < 0 or tax < 0 or total < 0:
            raise forms.ValidationError("Bill amounts cannot be negative.")
        # To the cent: a total a cent away from its parts would post a payable
        # the bill's own lines do not add up to, and the cent would fall into
        # an expense account with nothing to say where it came from.
        if (subtotal + tax) != total:
            self.add_error(
                "total_amount",
                f"Total must equal subtotal + tax exactly ({money(subtotal + tax)}), "
                f"not {money(total)}. The payable posted to the ledger must match the bill.",
            )
        on_order = "purchase_order" in self.fields and cleaned.get("purchase_order") \
            or (not is_new(self) and self.instance.purchase_order_id)
        if total == 0 and not on_order:
            # Only an order's free goods are billed at nothing ("Bill this order"
            # settles those with no journal). A typed bill for nothing records
            # no debt and no cost; it is a mistake, not a document.
            self.add_error(
                "total_amount",
                "A bill for 0.00 records nothing. Enter the amounts on the supplier's "
                "invoice; free goods on a purchase order are settled from the order "
                "with \"Bill this order\".")
        # The same supplier invoice entered twice is the commonest way a bill
        # gets paid twice: refuse it while the first one still stands.
        supplier = cleaned.get("supplier")
        supplier_ref = (cleaned.get("supplier_reference") or "").strip()
        if supplier and supplier_ref and self.company:
            twin = Bill.objects.filter(
                company=self.company, supplier=supplier,
                supplier_reference__iexact=supplier_ref,
            ).exclude(status__in=["CANCELLED", "DEBIT_NOTE"]).exclude(
                pk=self.instance.pk).first()
            if twin:
                self.add_error(
                    "supplier_reference",
                    f"{supplier.name}'s invoice {supplier_ref} is already in the book as "
                    f"{twin.reference}. Cancel that bill first if this one replaces it.")
        return cleaned
