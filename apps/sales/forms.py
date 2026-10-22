"""Sales forms."""
import datetime
from decimal import Decimal

from django import forms
from django.db.models import Sum
from django.forms import inlineformset_factory

from apps.core import guards
from apps.core.scoping import narrow
from .models import Invoice, InvoiceLine, CustomerPayment


class InvoiceForm(guards.GuardedFormMixin, forms.ModelForm):
    class Meta:
        model = Invoice
        fields = ["reference", "customer", "invoice_date", "due_date", "currency", "notes"]
        widgets = {
            "invoice_date": forms.DateInput(attrs={"type": "date"}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        self.company = company
        narrow(self, company, customer="company")
        self.fields["customer"].queryset = self.fields["customer"].queryset.filter(is_active=True)
        if self.instance._state.adding:
            self.fields["invoice_date"].initial = datetime.date.today()
            self.fields["due_date"].initial = datetime.date.today() + datetime.timedelta(days=30)

    def clean_invoice_date(self):
        return guards.plausible_date(self.cleaned_data.get("invoice_date"), "Invoice date")

    def clean(self):
        data = super().clean()
        guards.order(self, "invoice_date", "due_date", "Due date can't be before the invoice date.")
        guards.unique(self, Invoice, self.company, "reference", "Invoice number")
        return data


class _LineRulesMixin(guards.GuardedFormMixin):
    """Quantities above zero, prices not negative, discounts 0–100%."""

    def clean_quantity(self):
        return guards.positive(self.cleaned_data.get("quantity"), "Quantity")

    def clean_unit_price(self):
        return guards.not_negative(self.cleaned_data.get("unit_price"), "Unit price")

    def clean_discount_percent(self):
        return guards.percent(self.cleaned_data.get("discount_percent"), "Discount")


class InvoiceLineForm(_LineRulesMixin, forms.ModelForm):
    class Meta:
        model = InvoiceLine
        fields = ["product", "description", "quantity", "unit_price", "discount_percent", "tax_rate"]

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        _narrow_line(self, company)
        self.fields["product"].empty_label = "— Custom line item"
        self.fields["tax_rate"].empty_label = "— No tax"


def _narrow_line(form, company):
    """Product / tax-rate dropdowns: active rows of `company` only (empty without one)."""
    narrow(form, company, product="company", tax_rate="company")
    for name in ("product", "tax_rate"):
        form.fields[name].queryset = form.fields[name].queryset.filter(is_active=True)


InvoiceLineFormSet = inlineformset_factory(
    Invoice,
    InvoiceLine,
    form=InvoiceLineForm,
    extra=3,
    min_num=1,
    validate_min=True,
    can_delete=True,
)


class CustomerPaymentForm(guards.GuardedFormMixin, forms.ModelForm):
    class Meta:
        model = CustomerPayment
        fields = ["reference", "customer", "payment_date", "amount", "payment_method", "transaction_reference", "notes"]
        widgets = {
            "payment_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        self.company = company
        narrow(self, company, customer="company")
        self.fields["customer"].queryset = self.fields["customer"].queryset.filter(is_active=True)
        if self.instance._state.adding:
            self.fields["payment_date"].initial = datetime.date.today()

    def clean_amount(self):
        return guards.positive(self.cleaned_data.get("amount"), "Amount received")

    def clean_payment_date(self):
        return guards.not_in_future(guards.plausible_date(self.cleaned_data.get("payment_date"), "Payment date"),
                                    "Payment date")

    def clean(self):
        data = super().clean()
        guards.unique(self, CustomerPayment, self.company, "reference", "Receipt number")
        customer, amount = data.get("customer"), data.get("amount")
        if customer and amount and amount.is_finite() and self.instance._state.adding:
            owed = customer.invoices.filter(status__in=["SENT", "PARTIAL", "OVERDUE"]).aggregate(
                t=Sum("outstanding_amount"))["t"] or Decimal("0")
            if amount > owed:
                self.add_error("amount", f"{customer.name} owes {owed:,.2f}. Record the receipt against what is "
                                         f"owed; an advance or overpayment needs a credit note or refund instead.")
        return data


class SalesOrderForm(guards.GuardedFormMixin, forms.ModelForm):
    class Meta:
        model = __import__('apps.sales.models', fromlist=['SalesOrder']).SalesOrder
        fields = ["reference", "customer", "order_date", "delivery_date", "warehouse",
                  "sales_rep", "currency", "discount_percent", "branch", "notes"]
        widgets = {
            "order_date": forms.DateInput(attrs={"type": "date"}),
            "delivery_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        narrow(self, company, customer="company", warehouse="company", branch="company",
               sales_rep="users")
        self.fields["customer"].queryset = self.fields["customer"].queryset.filter(is_active=True)

    def clean_discount_percent(self):
        return guards.percent(self.cleaned_data.get("discount_percent"), "Discount")

    def clean(self):
        data = super().clean()
        guards.order(self, "order_date", "delivery_date", "Delivery date can't be before the order date.")
        return data


class SalesOrderLineForm(_LineRulesMixin, forms.ModelForm):
    class Meta:
        model = __import__('apps.sales.models', fromlist=['SalesOrderLine']).SalesOrderLine
        fields = ["product", "description", "quantity", "unit_price", "discount_percent", "tax_rate"]

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        _narrow_line(self, company)
