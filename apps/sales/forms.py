"""Sales forms."""
import datetime
from django import forms
from django.forms import inlineformset_factory

from apps.core.scoping import narrow
from .models import Invoice, InvoiceLine, CustomerPayment


class InvoiceForm(forms.ModelForm):
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
        narrow(self, company, customer="company")
        self.fields["customer"].queryset = self.fields["customer"].queryset.filter(is_active=True)
        if not self.instance.pk:
            self.fields["invoice_date"].initial = datetime.date.today()
            self.fields["due_date"].initial = datetime.date.today() + datetime.timedelta(days=30)


class InvoiceLineForm(forms.ModelForm):
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


class CustomerPaymentForm(forms.ModelForm):
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
        narrow(self, company, customer="company")
        self.fields["customer"].queryset = self.fields["customer"].queryset.filter(is_active=True)
        if not self.instance.pk:
            self.fields["payment_date"].initial = datetime.date.today()


class SalesOrderForm(forms.ModelForm):
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


class SalesOrderLineForm(forms.ModelForm):
    class Meta:
        model = __import__('apps.sales.models', fromlist=['SalesOrderLine']).SalesOrderLine
        fields = ["product", "description", "quantity", "unit_price", "discount_percent", "tax_rate"]

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        _narrow_line(self, company)
