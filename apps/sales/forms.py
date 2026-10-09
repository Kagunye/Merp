"""Sales forms."""
import datetime
from django import forms
from django.forms import inlineformset_factory
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
        if company:
            from apps.crm.models import Customer
            self.fields["customer"].queryset = Customer.objects.filter(company=company, is_active=True)
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
        fields = ["reference", "customer", "payment_date", "amount", "payment_method", "transaction_reference", "notes"]
        widgets = {
            "payment_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        if company:
            from apps.crm.models import Customer
            self.fields["customer"].queryset = Customer.objects.filter(company=company, is_active=True)
        if not self.instance.pk:
            self.fields["payment_date"].initial = datetime.date.today()
