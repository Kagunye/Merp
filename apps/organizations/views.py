from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView, DetailView, ListView, TemplateView, UpdateView,
)

from .forms import BranchForm, CompanyForm, DepartmentForm, WarehouseForm
from .models import Branch, Company, Department, Warehouse


def companies_for_user(user):
    """The companies `user` may see. System admins see the whole estate."""
    if user.is_system_admin or user.is_superuser:
        return Company.objects.all()
    return Company.objects.filter(
        memberships__user=user, memberships__is_active=True,
    ).distinct()


class ModuleHomeView(LoginRequiredMixin, TemplateView):
    template_name = "organizations/home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Organizations"
        ctx["companies"] = companies_for_user(self.request.user)
        return ctx


# ---- Company CRUD ----
class CompanyListView(LoginRequiredMixin, ListView):
    model = Company
    template_name = "organizations/company_list.html"
    context_object_name = "companies"

    def get_queryset(self):
        return companies_for_user(self.request.user)


class CompanyCreateView(LoginRequiredMixin, CreateView):
    model = Company
    form_class = CompanyForm
    template_name = "organizations/company_form.html"
    success_url = reverse_lazy("organizations:company_list")

    def form_valid(self, form):
        with transaction.atomic():
            self.object = form.save()
            from apps.core.services import provision_company
            provision_company(self.object, self.request.user)
        messages.success(
            self.request,
            f"Company '{self.object.name}' created with head office, chart of accounts, "
            "current fiscal year and VAT 16% ready to use.",
        )
        return redirect(self.get_success_url())


class CompanyUpdateView(LoginRequiredMixin, UpdateView):
    model = Company
    form_class = CompanyForm
    template_name = "organizations/company_form.html"
    success_url = reverse_lazy("organizations:company_list")

    def get_queryset(self):
        return companies_for_user(self.request.user)


class CompanyDetailView(LoginRequiredMixin, DetailView):
    model = Company
    template_name = "organizations/company_detail.html"
    context_object_name = "company"

    def get_queryset(self):
        return companies_for_user(self.request.user)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["branches"] = self.object.branches.all()
        ctx["departments"] = self.object.departments.all()
        ctx["warehouses"] = self.object.warehouses.all()
        return ctx


# ---- Branch CRUD ----
class BranchListView(LoginRequiredMixin, ListView):
    model = Branch
    template_name = "organizations/branch_list.html"
    context_object_name = "branches"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Branch.objects.none()
        return Branch.objects.filter(company=company).select_related("company")


class BranchCreateView(LoginRequiredMixin, CreateView):
    model = Branch
    form_class = BranchForm
    template_name = "organizations/branch_form.html"
    success_url = reverse_lazy("organizations:branch_list")



    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs
class BranchUpdateView(LoginRequiredMixin, UpdateView):
    model = Branch
    form_class = BranchForm
    template_name = "organizations/branch_form.html"
    success_url = reverse_lazy("organizations:branch_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Branch.objects.none()
        return Branch.objects.filter(company=company)


class BranchDetailView(LoginRequiredMixin, DetailView):
    model = Branch
    template_name = "organizations/branch_detail.html"
    context_object_name = "branch"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Branch.objects.none()
        return Branch.objects.filter(company=company)


# ---- Department CRUD ----

class DepartmentListView(LoginRequiredMixin, ListView):
    model = Department
    template_name = "organizations/department_list.html"
    context_object_name = "departments"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Department.objects.none()
        return Department.objects.filter(company=company).select_related("branch")


class DepartmentCreateView(LoginRequiredMixin, CreateView):
    model = Department
    form_class = DepartmentForm
    template_name = "organizations/department_form.html"
    success_url = reverse_lazy("organizations:department_list")



    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs
class DepartmentUpdateView(LoginRequiredMixin, UpdateView):
    model = Department
    form_class = DepartmentForm
    template_name = "organizations/department_form.html"
    success_url = reverse_lazy("organizations:department_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Department.objects.none()
        return Department.objects.filter(company=company)


# ---- Warehouse CRUD ----

class WarehouseListView(LoginRequiredMixin, ListView):
    model = Warehouse
    template_name = "organizations/warehouse_list.html"
    context_object_name = "warehouses"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Warehouse.objects.none()
        return Warehouse.objects.filter(company=company).select_related("branch")


class WarehouseCreateView(LoginRequiredMixin, CreateView):
    model = Warehouse
    form_class = WarehouseForm
    template_name = "organizations/warehouse_form.html"
    success_url = reverse_lazy("organizations:warehouse_list")



    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs
class WarehouseUpdateView(LoginRequiredMixin, UpdateView):
    model = Warehouse
    form_class = WarehouseForm
    template_name = "organizations/warehouse_form.html"
    success_url = reverse_lazy("organizations:warehouse_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Warehouse.objects.none()
        return Warehouse.objects.filter(company=company)


class WarehouseDetailView(LoginRequiredMixin, DetailView):
    model = Warehouse
    template_name = "organizations/warehouse_detail.html"
    context_object_name = "warehouse"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Warehouse.objects.none()
        return Warehouse.objects.filter(company=company)

