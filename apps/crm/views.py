"""CRM views."""
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from .forms import CustomerForm, LeadForm, SupplierForm
from .models import Customer, Lead, Supplier


class CustomerListView(LoginRequiredMixin, ListView):
    template_name = "crm/customer_list.html"
    context_object_name = "customers"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Customer.objects.none()
        qs = Customer.objects.filter(company=company, is_active=True)
        q = self.request.GET.get("q")
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(email__icontains=q) | Q(phone__icontains=q))
        ctype = self.request.GET.get("type")
        if ctype:
            qs = qs.filter(customer_type=ctype)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Customers"
        ctx["q"] = self.request.GET.get("q", "")
        ctx["selected_type"] = self.request.GET.get("type", "")
        ctx["customer_types"] = Customer.CUSTOMER_TYPES
        return ctx


class CustomerCreateView(LoginRequiredMixin, CreateView):
    model = Customer
    form_class = CustomerForm
    template_name = "crm/customer_form.html"
    success_url = reverse_lazy("crm:customer_list")

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        messages.success(self.request, f"Customer '{form.instance.name}' created.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "New Customer"
        ctx["action"] = "Create Customer"
        return ctx


class CustomerUpdateView(LoginRequiredMixin, UpdateView):
    model = Customer
    form_class = CustomerForm
    template_name = "crm/customer_form.html"
    success_url = reverse_lazy("crm:customer_list")

    def get_queryset(self):
        return Customer.objects.filter(company=self.request.active_company)

    def form_valid(self, form):
        messages.success(self.request, f"Customer '{form.instance.name}' updated.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit: {self.object.name}"
        ctx["action"] = "Save Changes"
        return ctx


class CustomerDetailView(LoginRequiredMixin, DetailView):
    model = Customer
    template_name = "crm/customer_detail.html"
    context_object_name = "customer"

    def get_queryset(self):
        return Customer.objects.filter(company=self.request.active_company)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = self.object.name
        ctx["invoices"] = self.object.invoices.order_by("-invoice_date")[:10]
        ctx["payments"] = self.object.payments.order_by("-payment_date")[:10]
        return ctx


class SupplierListView(LoginRequiredMixin, ListView):
    template_name = "crm/supplier_list.html"
    context_object_name = "suppliers"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Supplier.objects.none()
        qs = Supplier.objects.filter(company=company, is_active=True)
        q = self.request.GET.get("q")
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(email__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Suppliers"
        ctx["q"] = self.request.GET.get("q", "")
        return ctx


class SupplierCreateView(LoginRequiredMixin, CreateView):
    model = Supplier
    form_class = SupplierForm
    template_name = "crm/supplier_form.html"
    success_url = reverse_lazy("crm:supplier_list")

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        messages.success(self.request, f"Supplier '{form.instance.name}' created.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "New Supplier"
        ctx["action"] = "Create Supplier"
        return ctx


class SupplierUpdateView(LoginRequiredMixin, UpdateView):
    model = Supplier
    form_class = SupplierForm
    template_name = "crm/supplier_form.html"
    success_url = reverse_lazy("crm:supplier_list")

    def get_queryset(self):
        return Supplier.objects.filter(company=self.request.active_company)

    def form_valid(self, form):
        messages.success(self.request, f"Supplier '{form.instance.name}' updated.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit: {self.object.name}"
        ctx["action"] = "Save Changes"
        return ctx


class LeadListView(LoginRequiredMixin, ListView):
    template_name = "crm/lead_list.html"
    context_object_name = "leads"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Lead.objects.none()
        qs = Lead.objects.filter(company=company, is_active=True)
        status = self.request.GET.get("status")
        if status:
            qs = qs.filter(status=status)
        q = self.request.GET.get("q")
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(company_name__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Leads"
        ctx["q"] = self.request.GET.get("q", "")
        ctx["status_filter"] = self.request.GET.get("status", "")
        ctx["status_choices"] = Lead.STATUS_CHOICES
        return ctx


class LeadCreateView(LoginRequiredMixin, CreateView):
    model = Lead
    form_class = LeadForm
    template_name = "crm/lead_form.html"
    success_url = reverse_lazy("crm:lead_list")

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        from apps.accounts.models import User
        company = self.request.active_company
        if company:
            from apps.organizations.models import CompanyMembership
            user_ids = CompanyMembership.objects.filter(company=company).values_list("user_id", flat=True)
            form.fields["assigned_to"].queryset = User.objects.filter(id__in=user_ids)
        return form

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        messages.success(self.request, f"Lead '{form.instance.name}' created.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "New Lead"
        ctx["action"] = "Create Lead"
        return ctx


class SupplierDetailView(LoginRequiredMixin, DetailView):
    model = __import__('apps.crm.models', fromlist=['Supplier']).Supplier
    template_name = "crm/supplier_detail.html"
    context_object_name = "supplier"


class LeadDetailView(LoginRequiredMixin, DetailView):
    model = __import__('apps.crm.models', fromlist=['Lead']).Lead
    template_name = "crm/lead_detail.html"
    context_object_name = "lead"
