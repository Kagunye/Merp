from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView, DetailView, ListView, TemplateView, UpdateView,
)

from apps.core.access import is_platform_admin
from apps.core.scoping import CompanyScopedMixin, narrow

from .forms import BranchForm, CompanyBrandingForm, CompanyForm, DepartmentForm, WarehouseForm
from .models import Branch, Company, CompanyMembership, Department, Warehouse


def _my_companies(user):
    """Companies the user belongs to (platform admins see every company)."""
    if is_platform_admin(user):
        return Company.objects.all()
    return Company.objects.filter(
        memberships__user=user, memberships__is_active=True,
    ).distinct()


class ModuleHomeView(LoginRequiredMixin, TemplateView):
    template_name = "organizations/home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Organizations"
        ctx["companies"] = _my_companies(self.request.user)
        return ctx


# ---- Company ----
class CompanyListView(LoginRequiredMixin, ListView):
    model = Company
    template_name = "organizations/company_list.html"
    context_object_name = "companies"

    def get_queryset(self):
        return _my_companies(self.request.user)


class CompanyCreateView(LoginRequiredMixin, CreateView):
    """Anyone signed in can start a company; they become its administrator."""
    model = Company
    form_class = CompanyForm
    template_name = "organizations/company_form.html"

    def form_valid(self, form):
        with transaction.atomic():
            self.object = form.save()
            has_default = CompanyMembership.objects.filter(user=self.request.user, is_default=True).exists()
            CompanyMembership.objects.create(
                company=self.object, user=self.request.user, role="ADMIN", is_default=not has_default,
            )
        self.request.session["active_company_id"] = str(self.object.id)
        self.request.session.pop("active_branch_id", None)
        messages.success(self.request, f"{self.object.name} is ready. You're its administrator.")
        return redirect("dashboard:index")


class _ActiveCompanyMixin:
    """Edit views that only ever act on the company you're working in."""

    def get_object(self, queryset=None):
        active = getattr(self.request, "active_company", None)
        pk = self.kwargs.get("pk")
        if not active or (pk and str(pk) != str(active.pk)):
            raise Http404("Switch to that company to change its settings.")
        return active


class CompanyUpdateView(_ActiveCompanyMixin, LoginRequiredMixin, UpdateView):
    model = Company
    form_class = CompanyForm
    template_name = "organizations/company_form.html"
    success_url = reverse_lazy("organizations:company_list")


class CompanyBrandingView(_ActiveCompanyMixin, LoginRequiredMixin, UpdateView):
    """Edit logo, colours, letterhead header/footer and bank-details block."""
    model = Company
    form_class = CompanyBrandingForm
    template_name = "organizations/branding_form.html"

    def get_success_url(self):
        return reverse_lazy("organizations:branding")

    def form_valid(self, form):
        messages.success(self.request, "Branding saved. Documents will reflect the new theme.")
        return super().form_valid(form)


class CompanyDetailView(LoginRequiredMixin, DetailView):
    model = Company
    template_name = "organizations/company_detail.html"
    context_object_name = "company"

    def get_queryset(self):
        return _my_companies(self.request.user)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["branches"] = self.object.branches.all()
        ctx["departments"] = self.object.departments.all()
        ctx["warehouses"] = self.object.warehouses.all()
        return ctx


# ---- Branches, departments, warehouses: always the active company ----
class _CompanyOwnedFormMixin:
    """Sets `company` from the active company and narrows related dropdowns."""
    narrow_fields = {}

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        return narrow(form, self.request.active_company, **self.narrow_fields)

    def form_valid(self, form):
        from django.db import IntegrityError
        form.instance.company = self.request.active_company
        try:
            with transaction.atomic():
                return super().form_valid(form)
        except IntegrityError:
            form.add_error("name" if "name" in form.fields else None,
                           "Your company already has one with this name or code.")
            return self.form_invalid(form)


class BranchListView(CompanyScopedMixin, LoginRequiredMixin, ListView):
    model = Branch
    template_name = "organizations/branch_list.html"
    context_object_name = "branches"


class BranchCreateView(_CompanyOwnedFormMixin, LoginRequiredMixin, CreateView):
    model = Branch
    form_class = BranchForm
    template_name = "organizations/branch_form.html"
    success_url = reverse_lazy("organizations:branch_list")


class BranchUpdateView(_CompanyOwnedFormMixin, CompanyScopedMixin, LoginRequiredMixin, UpdateView):
    model = Branch
    form_class = BranchForm
    template_name = "organizations/branch_form.html"
    success_url = reverse_lazy("organizations:branch_list")


class BranchDetailView(CompanyScopedMixin, LoginRequiredMixin, DetailView):
    model = Branch
    template_name = "organizations/branch_detail.html"
    context_object_name = "branch"


class DepartmentListView(CompanyScopedMixin, LoginRequiredMixin, ListView):
    model = Department
    template_name = "organizations/department_list.html"
    context_object_name = "departments"

    def get_queryset(self):
        return super().get_queryset().select_related("branch")


class DepartmentCreateView(_CompanyOwnedFormMixin, LoginRequiredMixin, CreateView):
    model = Department
    form_class = DepartmentForm
    template_name = "organizations/department_form.html"
    success_url = reverse_lazy("organizations:department_list")
    narrow_fields = {"branch": "company", "parent": "company"}


class DepartmentUpdateView(_CompanyOwnedFormMixin, CompanyScopedMixin, LoginRequiredMixin, UpdateView):
    model = Department
    form_class = DepartmentForm
    template_name = "organizations/department_form.html"
    success_url = reverse_lazy("organizations:department_list")
    narrow_fields = {"branch": "company", "parent": "company"}


class WarehouseListView(CompanyScopedMixin, LoginRequiredMixin, ListView):
    model = Warehouse
    template_name = "organizations/warehouse_list.html"
    context_object_name = "warehouses"

    def get_queryset(self):
        return super().get_queryset().select_related("branch")


class WarehouseCreateView(_CompanyOwnedFormMixin, LoginRequiredMixin, CreateView):
    model = Warehouse
    form_class = WarehouseForm
    template_name = "organizations/warehouse_form.html"
    success_url = reverse_lazy("organizations:warehouse_list")
    narrow_fields = {"branch": "company"}


class WarehouseUpdateView(_CompanyOwnedFormMixin, CompanyScopedMixin, LoginRequiredMixin, UpdateView):
    model = Warehouse
    form_class = WarehouseForm
    template_name = "organizations/warehouse_form.html"
    success_url = reverse_lazy("organizations:warehouse_list")
    narrow_fields = {"branch": "company"}


class WarehouseDetailView(CompanyScopedMixin, LoginRequiredMixin, DetailView):
    model = Warehouse
    template_name = "organizations/warehouse_detail.html"
    context_object_name = "warehouse"
