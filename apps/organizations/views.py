from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView, DetailView, ListView, TemplateView, UpdateView,
)

from .forms import BranchForm, CompanyForm, DepartmentForm, WarehouseForm
from .models import Branch, Company, Department, Warehouse


class ModuleHomeView(LoginRequiredMixin, TemplateView):
    template_name = "organizations/home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Organizations"
        ctx["companies"] = Company.objects.all()
        return ctx


# ---- Company CRUD ----
class CompanyListView(LoginRequiredMixin, ListView):
    model = Company
    template_name = "organizations/company_list.html"
    context_object_name = "companies"


class CompanyCreateView(LoginRequiredMixin, CreateView):
    model = Company
    form_class = CompanyForm
    template_name = "organizations/company_form.html"
    success_url = reverse_lazy("organizations:company_list")

    def form_valid(self, form):
        messages.success(self.request, "Company created.")
        return super().form_valid(form)


class CompanyUpdateView(LoginRequiredMixin, UpdateView):
    model = Company
    form_class = CompanyForm
    template_name = "organizations/company_form.html"
    success_url = reverse_lazy("organizations:company_list")


class CompanyDetailView(LoginRequiredMixin, DetailView):
    model = Company
    template_name = "organizations/company_detail.html"
    context_object_name = "company"

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
        return Branch.objects.select_related("company")


class BranchCreateView(LoginRequiredMixin, CreateView):
    model = Branch
    form_class = BranchForm
    template_name = "organizations/branch_form.html"
    success_url = reverse_lazy("organizations:branch_list")


class BranchUpdateView(LoginRequiredMixin, UpdateView):
    model = Branch
    form_class = BranchForm
    template_name = "organizations/branch_form.html"
    success_url = reverse_lazy("organizations:branch_list")


class BranchDetailView(LoginRequiredMixin, DetailView):
    model = Branch
    template_name = "organizations/branch_detail.html"
    context_object_name = "branch"


# ---- Department CRUD ----
class DepartmentListView(LoginRequiredMixin, ListView):
    model = Department
    template_name = "organizations/department_list.html"
    context_object_name = "departments"

    def get_queryset(self):
        return Department.objects.select_related("company", "branch")


class DepartmentCreateView(LoginRequiredMixin, CreateView):
    model = Department
    form_class = DepartmentForm
    template_name = "organizations/department_form.html"
    success_url = reverse_lazy("organizations:department_list")


class DepartmentUpdateView(LoginRequiredMixin, UpdateView):
    model = Department
    form_class = DepartmentForm
    template_name = "organizations/department_form.html"
    success_url = reverse_lazy("organizations:department_list")


# ---- Warehouse CRUD ----
class WarehouseListView(LoginRequiredMixin, ListView):
    model = Warehouse
    template_name = "organizations/warehouse_list.html"
    context_object_name = "warehouses"

    def get_queryset(self):
        return Warehouse.objects.select_related("company", "branch")


class WarehouseCreateView(LoginRequiredMixin, CreateView):
    model = Warehouse
    form_class = WarehouseForm
    template_name = "organizations/warehouse_form.html"
    success_url = reverse_lazy("organizations:warehouse_list")


class WarehouseUpdateView(LoginRequiredMixin, UpdateView):
    model = Warehouse
    form_class = WarehouseForm
    template_name = "organizations/warehouse_form.html"
    success_url = reverse_lazy("organizations:warehouse_list")


class WarehouseDetailView(LoginRequiredMixin, DetailView):
    model = Warehouse
    template_name = "organizations/warehouse_detail.html"
    context_object_name = "warehouse"
