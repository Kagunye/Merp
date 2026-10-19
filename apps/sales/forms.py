"""Sales forms."""
import datetime
from django import forms
from apps.core.forms import date_input, is_new, lock_currency
from django.forms import inlineformset_factory
from .models import Invoice, InvoiceLine, CustomerPayment, SalesOrder, SalesOrderLine


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
        if company:
            from apps.crm.models import Customer
            self.fields["customer"].queryset = Customer.objects.filter(company=company, is_active=True)
            lock_currency(self, company)
        if is_new(self):
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
            "payment_date": date_input(),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        if company:
            from apps.crm.models import Customer
            self.fields["customer"].queryset = Customer.objects.filter(company=company, is_active=True)
            lock_currency(self, company)
        if is_new(self):
            self.fields["payment_date"].initial = datetime.date.today()


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
            lock_currency(self, company)
        if is_new(self):
            self.fields["order_date"].initial = datetime.date.today()


class SalesOrderLineForm(forms.ModelForm):
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
