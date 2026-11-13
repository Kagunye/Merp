from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView, DetailView, ListView, TemplateView, UpdateView,
)

from apps.core.access import is_platform_admin

from .forms import BranchForm, CompanyForm, DepartmentForm, WarehouseForm
from .models import Branch, Company, Department, Warehouse


def companies_for_user(user):
    """The companies `user` may see. System admins see the whole estate."""
    if user.is_system_admin or user.is_superuser:
        return Company.objects.all()
    return Company.objects.filter(
        memberships__user=user, memberships__is_active=True,
    ).distinct()


def companies_user_administers(user):
    """The companies whose own record `user` may rewrite.

    Being a member of a company is not the same as running it. The role table
    (apps.core.access) is decided by the user's role in the *active* company,
    so an ADMIN of company A who is only a VIEWER of company B would pass the
    role check for B's edit page while working in A. Editing a company is
    therefore limited here to the companies where the user's own membership
    is ADMIN; platform admins keep the whole estate.
    """
    if user.is_system_admin or user.is_superuser:
        return Company.objects.all()
    return Company.objects.filter(
        memberships__user=user, memberships__is_active=True, memberships__role="ADMIN",
    ).distinct()


class ModuleHomeView(LoginRequiredMixin, TemplateView):
    template_name = "organizations/home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Organizations"
        ctx["companies"] = companies_for_user(self.request.user)
        ctx["editable_ids"] = set(
            companies_user_administers(self.request.user).values_list("pk", flat=True))
        return ctx


# ---- Company CRUD ----
class CompanyListView(LoginRequiredMixin, ListView):
    model = Company
    template_name = "organizations/company_list.html"
    context_object_name = "companies"

    def get_queryset(self):
        return companies_for_user(self.request.user)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # Only offer "Edit" on the companies this person actually runs.
        ctx["editable_ids"] = set(
            companies_user_administers(self.request.user).values_list("pk", flat=True))
        return ctx


class CompanyCreateView(LoginRequiredMixin, CreateView):
    model = Company
    form_class = CompanyForm
    template_name = "organizations/company_form.html"
    success_url = reverse_lazy("organizations:company_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["platform_admin"] = is_platform_admin(self.request.user)
        return kwargs

    def form_valid(self, form):
        from apps.core import tenancy
        from apps.core.services import provision_company
        # A new company starts in the shared database, even when the one being
        # worked in keeps its data in a database of its own.
        with tenancy.company_db(None), transaction.atomic():
            self.object = form.save()
            provision_company(self.object, self.request.user)
        messages.success(
            self.request,
            f"Company '{self.object.name}' created with head office, chart of accounts, "
            "current fiscal year and VAT 16% ready to use.",
        )
        return redirect(self.get_success_url())


class CompanyUpdateView(LoginRequiredMixin, UpdateView):
    """Edit a company's own record: only for that company's ADMIN.

    The queryset is the companies the user administers, not every company
    they belong to, so a VIEWER seat in another company gives no way in: the
    page answers 404 there, exactly as for a company they never joined.
    """
    model = Company
    form_class = CompanyForm
    template_name = "organizations/company_form.html"
    success_url = reverse_lazy("organizations:company_list")

    def get_queryset(self):
        return companies_user_administers(self.request.user)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["platform_admin"] = is_platform_admin(self.request.user)
        return kwargs


class CompanyDetailView(LoginRequiredMixin, DetailView):
    model = Company
    template_name = "organizations/company_detail.html"
    context_object_name = "company"

    def get_queryset(self):
        return companies_for_user(self.request.user)

    def get_context_data(self, **kwargs):
        from apps.core import tenancy
        ctx = super().get_context_data(**kwargs)
        # Read from wherever this company keeps its data -- it may not be the
        # company being worked in -- so the lists are fetched here, not later.
        with tenancy.company_db(self.object):
            ctx["branches"] = list(self.object.branches.all())
            ctx["departments"] = list(self.object.departments.all())
            ctx["warehouses"] = list(self.object.warehouses.all())
        ctx["can_edit"] = companies_user_administers(self.request.user).filter(
            pk=self.object.pk).exists()
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



# ───────────────────────────── printed documents ─────────────────────────────

def document_layout(request):
    """The look of quotations, orders, invoices and purchase orders."""
    from django.contrib.auth.decorators import login_required  # noqa: F401  (middleware checks the role)
    from django.core.exceptions import ValidationError
    from django.shortcuts import render
    from .documents import layout_for, logo_url, save_logo
    from .forms import DocumentLayoutForm

    if not request.user.is_authenticated:
        return redirect("accounts:login")
    company = request.active_company
    if company is None:
        return redirect("organizations:company_list")
    layout = layout_for(company)
    form = DocumentLayoutForm(request.POST or None, request.FILES or None, instance=layout)
    if request.method == "POST":
        if request.POST.get("action") == "remove_logo":
            layout.logo_data, layout.logo_type, layout.logo_updated = None, "", None
            layout.save(update_fields=["logo_data", "logo_type", "logo_updated", "updated_at"])
            messages.success(request, "Logo removed from printed documents.")
            return redirect("organizations:document_layout")
        if form.is_valid():
            form.save()
            upload = form.cleaned_data.get("logo_file")
            if upload:
                try:
                    save_logo(layout, upload)
                except ValidationError as exc:
                    messages.error(request, exc.messages[0])
                    return redirect("organizations:document_layout")
            messages.success(request, "Document layout saved. Quotations, orders, invoices and purchase orders print with it.")
            return redirect("organizations:document_layout")
    from apps.sales.models import Invoice, SalesQuote
    sample = {"quote": SalesQuote.objects.filter(company=company).order_by("-created_at").first(),
              "invoice": Invoice.objects.filter(company=company, original_invoice__isnull=True).order_by("-created_at").first()}
    return render(request, "organizations/document_layout.html",
                  {"form": form, "layout": layout, "logo": logo_url(layout), "sample": sample})


def document_logo(request):
    from django.http import Http404, HttpResponse
    from .models import DocumentLayout
    company = getattr(request, "active_company", None)
    layout = DocumentLayout.objects.filter(company=company).first() if company else None
    if layout is None or not layout.logo_data:
        raise Http404
    response = HttpResponse(bytes(layout.logo_data), content_type=layout.logo_type or "image/png")
    response["Cache-Control"] = "private, max-age=31536000, immutable"
    return response
