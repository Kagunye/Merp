from decimal import Decimal

from django import forms
from apps.core.forms import date_input, is_new, pick_currency
from .models import Bill, PurchaseOrder, PurchaseRequisition


class PurchaseRequisitionForm(forms.ModelForm):
    class Meta:
        model = PurchaseRequisition
        fields = ["reference", "department", "required_date", "notes"]
        widgets = {
            "required_date": date_input(),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }


def chargeable_accounts(company):
    """The accounts a bill with no order behind it may be charged to.

    Expenses, costs and assets — a service, a utility, a piece of equipment —
    and Stock Received Not Invoiced, for goods that were taken into stock
    without an order and are only now being billed. The stock accounts
    themselves are left out on purpose: stock is valued by what is on the
    shelf, and a bill alone puts nothing on the shelf, so a bill coded to
    inventory would raise the account above the stock it is meant to describe.
    """
    from django.db.models import Q
    from apps.accounting.models import Account
    from apps.inventory.models import Product

    stock = set(Product.objects.filter(company=company, inventory_account__isnull=False)
                .values_list("inventory_account_id", flat=True))
    return (Account.objects.filter(company=company, is_active=True)
            .filter(Q(account_type__in=["EXPENSE", "COST_OF_GOODS", "ASSET"]) | Q(code="2110"))
            .exclude(code__in=["1400", "1450"]).exclude(pk__in=stock)
            .order_by("code"))


class BillForm(forms.ModelForm):
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
            "reference", "supplier", "purchase_order", "expense_account", "supplier_reference",
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
        self.fields["expense_account"].queryset = accounts
        # No blank choice on screen: every bill typed in is charged somewhere
        # the bookkeeper chose, and never to stock by default.
        self.fields["expense_account"].empty_label = None
        pick_currency(self, company)
        if is_new(self):
            import datetime
            # The order link is not offered on a new bill at all (see the class
            # note); clean() refuses one that is posted anyway.
            del self.fields["purchase_order"]
            default = accounts.filter(code="6000").first() or accounts.filter(
                account_type="EXPENSE").first()
            if default:
                self.fields["expense_account"].initial = default.pk
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
                self.fields["expense_account"].disabled = True
                self.fields["expense_account"].required = False

    def clean(self):
        cleaned = super().clean()
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
        if po:
            cleaned["expense_account"] = None
        subtotal = cleaned.get("subtotal") or Decimal("0")
        tax = cleaned.get("tax_amount") or Decimal("0")
        total = cleaned.get("total_amount")
        if total is None:
            return cleaned
        if subtotal < 0 or tax < 0 or total < 0:
            raise forms.ValidationError("Bill amounts cannot be negative.")
        if abs((subtotal + tax) - total) > Decimal("0.01"):
            self.add_error(
                "total_amount",
                f"Total must equal subtotal + tax ({subtotal + tax}). "
                "The payable posted to the ledger must match the bill.",
            )
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
