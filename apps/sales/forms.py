"""Sales forms."""
import datetime
from decimal import Decimal
from django import forms
from apps.core.forms import date_input, is_new, pick_currency
from django.forms import inlineformset_factory
from .models import Invoice, InvoiceLine, CustomerPayment, SalesOrder, SalesOrderLine


def _plain_percent(value):
    """7.375000 shown as 7.375 and 5.000000 as 5, so a stored six-place
    discount reads the way it was agreed and the browser's number box takes it."""
    if value in (None, ""):
        return value
    value = Decimal(str(value))
    return value.quantize(Decimal("1")) if value == value.to_integral() else value.normalize()


def _discount_in_range(value):
    """A discount is a share of the price given away: none of it up to all of it.

    A negative discount is a surcharge typed in the wrong box, and more than
    100% would bill the customer a negative price — revenue and VAT booked as
    money the business owes.
    """
    if value is None:
        return Decimal("0")
    if value < 0 or value > 100:
        raise forms.ValidationError(
            f"A discount runs from 0% to 100%; {value}% is not one. Charge "
            "extra by raising the unit price instead.")
    return value


class LineFiguresMixin:
    """The checks every invoice and order line passes before it is saved.

    A line is something sold: a positive quantity at a price that is not
    negative. Money given back is a credit note against the invoice that
    billed it; written as a minus line it would net revenue and VAT down
    without any document saying why.
    """

    def clean_quantity(self):
        quantity = self.cleaned_data.get("quantity")
        if quantity is not None and quantity <= 0:
            raise forms.ValidationError(
                "The quantity has to be more than zero. To give goods or money "
                "back, issue a credit note against the invoice that billed them.")
        return quantity

    def clean_unit_price(self):
        price = self.cleaned_data.get("unit_price")
        if price is not None and price < 0:
            raise forms.ValidationError(
                "The unit price cannot be negative. To reduce what a customer "
                "owes, issue a credit note or give a discount on the line.")
        return price

    def clean_discount_percent(self):
        return _discount_in_range(self.cleaned_data.get("discount_percent"))


def _reference_taken(model, company, reference, exclude_pk=None):
    """Is this reference already on another document of the company's?"""
    if not company or not reference:
        return False
    qs = model.objects.filter(company=company, reference__iexact=reference.strip())
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


class InvoiceForm(forms.ModelForm):
    class Meta:
        model = Invoice
        fields = ["reference", "customer", "invoice_date", "due_date", "currency",
                  "warehouse", "notes"]
        widgets = {
            "invoice_date": date_input(),
            "due_date": date_input(),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        self.company = company
        self.fields["warehouse"].required = False
        # Left blank, the next INV- number is drawn when the invoice is saved —
        # the form says so in its placeholder.
        self.fields["reference"].required = False
        if company:
            from apps.crm.models import Customer
            from apps.organizations.models import Warehouse
            self.fields["customer"].queryset = Customer.objects.filter(company=company, is_active=True)
            self.fields["warehouse"].queryset = Warehouse.objects.filter(company=company).order_by("name")
            self.fields["warehouse"].empty_label = "— Company default warehouse"
            pick_currency(self, company)
        if is_new(self):
            self.fields["invoice_date"].initial = datetime.date.today()
            self.fields["due_date"].initial = datetime.date.today() + datetime.timedelta(days=30)

    def clean_reference(self):
        """One number, one invoice. A second invoice under a number the company
        has already issued is either the same sale typed twice — a double
        charge on the customer's account — or a paper nobody can tell apart
        from the first when the customer quotes it."""
        reference = (self.cleaned_data.get("reference") or "").strip()
        if _reference_taken(Invoice, self.company or getattr(self.instance, "company", None),
                            reference, exclude_pk=self.instance.pk):
            raise forms.ValidationError(
                f"Invoice number {reference} is already used in this company. "
                "Leave the number to the system or pick one that is not taken.")
        return reference

    def clean(self):
        """An invoice cannot fall due before it was raised.

        A due date ahead of the invoice date makes the paper overdue the day it
        is written, so it would land in the ageing's late columns and draw a
        reminder before the customer has even received it — almost always a
        mistyped year or month.
        """
        cleaned = super().clean()
        raised, due = cleaned.get("invoice_date"), cleaned.get("due_date")
        if raised and due and due < raised:
            self.add_error(
                "due_date",
                f"The due date ({due:%d %b %Y}) is before the invoice date "
                f"({raised:%d %b %Y}). An invoice falls due on or after the day it "
                "is raised — check the year and month.")
        return cleaned


class InvoiceLineForm(LineFiguresMixin, forms.ModelForm):
    class Meta:
        model = InvoiceLine
        fields = ["product", "description", "quantity", "unit_price", "discount_percent", "tax_rate"]

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        if "discount_percent" in self.initial:
            self.initial["discount_percent"] = _plain_percent(self.initial["discount_percent"])
        if company:
            from apps.inventory.models import Product
            from apps.accounting.models import TaxRate
            self.fields["product"].queryset = Product.objects.filter(company=company, is_active=True)
            self.fields["product"].empty_label = "— Custom line item"
            self.fields["tax_rate"].queryset = TaxRate.objects.filter(company=company, is_active=True)
            self.fields["tax_rate"].empty_label = "— No tax"


InvoiceLineFormSet = inlineformset_factory(
    Invoice,
    InvoiceLine,
    form=InvoiceLineForm,
    extra=3,
    min_num=1,
    validate_min=True,
    can_delete=True,
)


class CustomerPaymentForm(forms.ModelForm):
    class Meta:
        model = CustomerPayment
        fields = ["reference", "customer", "payment_date", "amount", "currency",
                  "payment_method", "bank_account", "transaction_reference", "notes"]
        widgets = {
            "payment_date": date_input(),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
        help_texts = {
            "bank_account": "Where the money went in. Cash receipts can be left "
                            "blank and are paid into the till account.",
        }

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        self.company = company
        # Left blank, the next PAY- number is drawn when the receipt is saved.
        # That is also why a double-submitted blank form needs its one-time
        # ticket: each copy would otherwise draw a number of its own.
        self.fields["reference"].required = False
        self.fields["reference"].help_text = "Leave blank to use the next receipt number."
        if company:
            from apps.crm.models import Customer
            self.fields["customer"].queryset = Customer.objects.filter(company=company, is_active=True)
            from apps.banking.models import BankAccount
            self.fields["bank_account"].queryset = BankAccount.objects.filter(
                company=company, is_active=True)
            pick_currency(self, company)
        if is_new(self):
            self.fields["payment_date"].initial = datetime.date.today()

    def clean_amount(self):
        """A receipt is money coming in. A negative or nil figure books an entry
        the ledger cannot balance, and is far more likely a mistyped decimal."""
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError(
                "A receipt has to be for more than nothing — a payment going out "
                "is booked as a supplier payment, not a customer receipt.")
        return amount

    def clean_reference(self):
        """A receipt number is issued once. The same form sent twice — a double
        click, a refresh after a slow save — would otherwise book the money in
        the bank twice and clear the customer's invoices with cash that only
        came in once."""
        reference = (self.cleaned_data.get("reference") or "").strip()
        if _reference_taken(CustomerPayment, self.company, reference,
                            exclude_pk=self.instance.pk):
            raise forms.ValidationError(
                f"Receipt {reference} is already recorded in this company. If this "
                "is a different payment, give it its own number.")
        return reference

    def clean(self):
        """An M-Pesa confirmation code names one transfer. A second live receipt
        quoting the same code is the same money recorded again."""
        cleaned = super().clean()
        code = (cleaned.get("transaction_reference") or "").strip()
        if self.company and code and cleaned.get("payment_method") == "MPESA":
            clash = CustomerPayment.objects.filter(
                company=self.company, payment_method="MPESA", status="RECEIVED",
                transaction_reference__iexact=code).exclude(pk=self.instance.pk).first()
            if clash:
                self.add_error(
                    "transaction_reference",
                    f"M-Pesa code {code} is already on receipt {clash.reference}. "
                    "Reverse that receipt first if it was recorded against the "
                    "wrong customer.")
        return cleaned


class SalesOrderForm(forms.ModelForm):
    class Meta:
        model = SalesOrder
        fields = ["reference", "customer", "order_date", "delivery_date", "warehouse",
                  "sales_rep", "currency", "discount_percent", "branch", "notes"]
        widgets = {
            "order_date": date_input(),
            "delivery_date": date_input(),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        if company:
            from apps.crm.models import Customer
            from apps.organizations.models import Warehouse, Branch, CompanyMembership
            from apps.accounts.models import User
            self.fields["customer"].queryset = Customer.objects.filter(company=company, is_active=True)
            self.fields["warehouse"].queryset = Warehouse.objects.filter(company=company)
            self.fields["branch"].queryset = Branch.objects.filter(company=company)
            self.fields["sales_rep"].queryset = User.objects.filter(
                memberships__company=company, is_active=True
            ).distinct()
            default_wh = self.fields["warehouse"].queryset.filter(is_default=True).first()
            if default_wh:
                self.fields["warehouse"].initial = default_wh.pk
            pick_currency(self, company)
        if is_new(self):
            self.fields["order_date"].initial = datetime.date.today()

    def clean_discount_percent(self):
        return _discount_in_range(self.cleaned_data.get("discount_percent"))


class SalesOrderLineForm(LineFiguresMixin, forms.ModelForm):
    class Meta:
        model = SalesOrderLine
        fields = ["product", "description", "quantity", "unit_price", "discount_percent", "tax_rate"]

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        if company:
            from apps.inventory.models import Product
            from apps.accounting.models import TaxRate
            self.fields["product"].queryset = Product.objects.filter(company=company, is_active=True)
            self.fields["tax_rate"].queryset = TaxRate.objects.filter(company=company, is_active=True)
            self.fields["tax_rate"].empty_label = "— No tax"


SalesOrderLineFormSet = inlineformset_factory(
    SalesOrder,
    SalesOrderLine,
    form=SalesOrderLineForm,
    extra=3,
    min_num=1,
    validate_min=True,
    can_delete=True,
)
