"""Sales forms."""
import datetime
from decimal import Decimal
from django import forms
from apps.core.forms import date_input, is_new, open_date, pick_currency
from django.forms import inlineformset_factory
from .models import Invoice, InvoiceLine, CustomerPayment, SalesOrder, SalesOrderLine, SalesQuote, SalesQuoteLine


class InvoiceForm(forms.ModelForm):
    class Meta:
        model = Invoice
        fields = ["reference", "customer", "invoice_date", "due_date", "currency", "notes"]
        widgets = {
            "invoice_date": date_input(),
            "due_date": date_input(),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        self.company = company
        if company:
            from apps.crm.models import Customer
            self.fields["customer"].queryset = Customer.objects.filter(company=company, is_active=True)
            pick_currency(self, company)
        if is_new(self):
            self.fields["invoice_date"].initial = datetime.date.today()
            self.fields["due_date"].initial = datetime.date.today() + datetime.timedelta(days=30)

    def clean_invoice_date(self):
        return open_date(self.company, self.cleaned_data.get("invoice_date"), "invoice")


class PostedInvoiceForm(forms.ModelForm):
    """What may still change on an invoice the ledger already carries.

    The customer, dates, currency and lines are what the journal entry was
    written from; changing them here would leave the document and the ledger
    telling different stories. A credit note or a cancellation corrects those.
    """
    class Meta:
        model = Invoice
        fields = ["due_date", "notes"]
        widgets = {"due_date": date_input(), "notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)

    def clean_due_date(self):
        due = self.cleaned_data.get("due_date")
        if due and due < self.instance.invoice_date:
            raise forms.ValidationError("The due date can't be before the invoice date.")
        return due


class InvoiceLineForm(forms.ModelForm):
    class Meta:
        model = InvoiceLine
        fields = ["product", "description", "quantity", "unit_price", "discount_percent", "tax_rate"]

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
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
        if company:
            from apps.crm.models import Customer
            self.fields["customer"].queryset = Customer.objects.filter(company=company, is_active=True)
            from apps.banking.models import BankAccount
            self.fields["bank_account"].queryset = BankAccount.objects.filter(
                company=company, is_active=True)
            pick_currency(self, company)
        if is_new(self):
            self.fields["payment_date"].initial = datetime.date.today()

    def clean_payment_date(self):
        return open_date(self.company, self.cleaned_data.get("payment_date"), "payment")

    def clean_amount(self):
        """A receipt is money coming in. A negative or nil figure books an entry
        the ledger cannot balance, and is far more likely a mistyped decimal."""
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError(
                "A receipt has to be for more than nothing — a payment going out "
                "is booked as a supplier payment, not a customer receipt.")
        return amount


class SalesQuoteForm(forms.ModelForm):
    class Meta:
        model = SalesQuote
        fields = ["reference", "customer", "quote_date", "valid_until", "warehouse",
                  "sales_rep", "currency", "discount_percent", "branch", "notes"]
        widgets = {
            "quote_date": date_input(),
            "valid_until": date_input(),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        self.fields["reference"].required = False
        if company:
            from apps.crm.models import Customer
            from apps.organizations.models import Warehouse, Branch
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
            self.fields["quote_date"].initial = datetime.date.today()
            self.fields["valid_until"].initial = datetime.date.today() + datetime.timedelta(days=30)


class OptionalLineMixin:
    """Lines are optional: a row with no product, description or price is skipped."""

    def has_changed(self):
        if self.instance.pk is None:
            filled = any(
                (self.data.get(self.add_prefix(f)) or "").strip() not in ("", "0", "0.00")
                for f in ("product", "description", "unit_price")
            )
            if not filled:
                return False
        return super().has_changed()

    def clean(self):
        cleaned = super().clean()
        if self.cleaned_data.get("DELETE"):
            return cleaned
        if not cleaned.get("product"):
            self.add_error("product", "Choose a product, or remove this line.")
        if cleaned.get("quantity") in (None, ""):
            self.add_error("quantity", "Enter a quantity.")
        if cleaned.get("unit_price") is None:
            cleaned["unit_price"] = Decimal("0")
        if cleaned.get("discount_percent") is None:
            cleaned["discount_percent"] = Decimal("0")
        return cleaned


class SalesQuoteLineForm(OptionalLineMixin, forms.ModelForm):
    class Meta:
        model = SalesQuoteLine
        fields = ["product", "description", "quantity", "unit_price", "discount_percent", "tax_rate"]

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        self.fields["product"].required = False
        self.fields["quantity"].required = False
        self.fields["unit_price"].required = False
        self.fields["discount_percent"].required = False
        if company:
            from apps.inventory.models import Product
            from apps.accounting.models import TaxRate
            self.fields["product"].queryset = Product.objects.filter(company=company, is_active=True)
            self.fields["tax_rate"].queryset = TaxRate.objects.filter(company=company, is_active=True)
            self.fields["tax_rate"].empty_label = "— No tax"


SalesQuoteLineFormSet = inlineformset_factory(
    SalesQuote,
    SalesQuoteLine,
    form=SalesQuoteLineForm,
    extra=3,
    min_num=0,
    validate_min=False,
    can_delete=True,
)


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
        self.fields["reference"].required = False
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


class SalesOrderLineForm(OptionalLineMixin, forms.ModelForm):
    class Meta:
        model = SalesOrderLine
        fields = ["product", "description", "quantity", "unit_price", "discount_percent", "tax_rate"]

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        self.fields["product"].required = False
        self.fields["quantity"].required = False
        self.fields["unit_price"].required = False
        self.fields["discount_percent"].required = False
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
    min_num=0,
    validate_min=False,
    can_delete=True,
)
