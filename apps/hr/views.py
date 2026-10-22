"""HR views."""
import datetime
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.shortcuts import redirect
from django.utils import timezone
from django.views.generic import CreateView, DetailView, ListView, TemplateView, UpdateView

from apps.core.scoping import CompanyScopedMixin, get_scoped_or_404, narrow

from .forms import ContractForm, EmployeeForm, LeaveRequestForm
from .models import Employee, LeaveRequest, LeaveType


class EmployeeListView(LoginRequiredMixin, ListView):
    template_name = "hr/employee_list.html"
    context_object_name = "employees"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Employee.objects.none()
        qs = Employee.objects.filter(company=company, is_active=True).select_related("department")
        status = self.request.GET.get("status")
        if status:
            qs = qs.filter(status=status)
        q = self.request.GET.get("q")
        if q:
            qs = qs.filter(
                Q(first_name__icontains=q) | Q(last_name__icontains=q) |
                Q(employee_number__icontains=q) | Q(job_title__icontains=q)
            )
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Employees"
        ctx["status_choices"] = Employee.STATUS_CHOICES
        ctx["status_filter"] = self.request.GET.get("status", "")
        ctx["q"] = self.request.GET.get("q", "")
        return ctx


def _style(form):
    from django import forms as dforms
    for field in form.fields.values():
        w = field.widget
        if isinstance(w, dforms.Select):
            w.attrs["class"] = "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
        elif isinstance(w, dforms.Textarea):
            w.attrs.update({"class": "form-textarea w-full border border-gray-300 rounded-lg px-3 py-2 text-sm", "rows": 3})
        else:
            w.attrs["class"] = "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
            if isinstance(w, dforms.DateInput):
                w.input_type = "date"
    return form


class EmployeeCreateView(LoginRequiredMixin, CreateView):
    model = Employee
    form_class = EmployeeForm
    template_name = "hr/employee_form.html"

    def get_success_url(self):
        from django.urls import reverse
        return reverse("hr:employee_detail", kwargs={"pk": self.object.pk})

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_form(self, form_class=None):
        form = _style(super().get_form(form_class))
        company = self.request.active_company
        if company and not form.is_bound:
            from apps.core.models import SequenceCounter
            form.fields["employee_number"].initial = SequenceCounter.next_number(company, "EMP-")
        form.fields["date_hired"].initial = datetime.date.today()
        return form

    def form_valid(self, form):
        from django.db import transaction
        from .models import Contract
        form.instance.company = self.request.active_company
        with transaction.atomic():
            self.object = form.save()
            salary = form.cleaned_data.get("basic_salary")
            if salary:
                Contract.objects.create(employee=self.object, contract_type=form.cleaned_data.get("contract_type") or "PERMANENT",
                                        start_date=self.object.date_hired, basic_salary=salary, is_current=True,
                                        job_title=self.object.job_title or "Staff", department=self.object.department)
        note = "" if form.cleaned_data.get("basic_salary") else " Add their salary before the next payroll."
        messages.success(self.request, f"Employee '{self.object.full_name}' created.{note}")
        return redirect(self.get_success_url())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "New Employee"
        return ctx


class ContractCreateView(LoginRequiredMixin, CreateView):
    """New contract or salary change. The previous contract stops being current."""
    form_class = ContractForm
    template_name = "hr/contract_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.employee = get_scoped_or_404(Employee, request, pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(company=self.request.active_company, employee=self.employee)
        return kwargs

    def get_form(self, form_class=None):
        form = _style(super().get_form(form_class))
        current = self.employee.contracts.filter(is_current=True).first()
        if not form.is_bound:
            form.fields["start_date"].initial = datetime.date.today()
            form.fields["job_title"].initial = self.employee.job_title
            form.fields["department"].initial = self.employee.department_id
            if current:
                form.fields["basic_salary"].initial = current.basic_salary
                form.fields["contract_type"].initial = current.contract_type
        return form

    def form_valid(self, form):
        from django.db import transaction
        with transaction.atomic():
            self.employee.contracts.filter(is_current=True).update(is_current=False)
            form.instance.employee = self.employee
            form.instance.is_current = True
            self.object = form.save()
        messages.success(self.request, f"{self.employee.full_name}: {self.object.get_contract_type_display().lower()} "
                                       f"contract at {self.object.basic_salary:,.2f} a month from "
                                       f"{self.object.start_date:%d %b %Y}.")
        return redirect("hr:employee_detail", pk=self.employee.pk)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["employee"] = self.employee
        ctx["page_title"] = f"Contract / salary — {self.employee.full_name}"
        return ctx


class EmployeeDetailView(CompanyScopedMixin, LoginRequiredMixin, DetailView):
    model = Employee
    template_name = "hr/employee_detail.html"
    context_object_name = "employee"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = self.object.full_name
        ctx["contracts"] = self.object.contracts.order_by("-start_date")[:5]
        ctx["leave_requests"] = self.object.leave_requests.order_by("-created_at")[:10]
        ctx["payslips"] = self.object.payslips.select_related("payroll_run").order_by("-created_at")[:10]
        from apps.core import access
        ctx["can_edit_contract"] = access.can(self.request.user, getattr(self.request, "company_role", None),
                                              "hr", "contract_create", "POST")
        return ctx


class LeaveRequestListView(LoginRequiredMixin, ListView):
    template_name = "hr/leave_list.html"
    context_object_name = "leaves"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return LeaveRequest.objects.none()
        qs = LeaveRequest.objects.filter(
            employee__company=company
        ).select_related("employee", "leave_type")
        # Non-staff see only their own
        if hasattr(self.request.user, "employee"):
            emp = self.request.user.employee
            if emp.company == company:
                status = self.request.GET.get("status")
                if status:
                    qs = qs.filter(status=status)
                return qs.filter(employee=emp)
        status = self.request.GET.get("status")
        if status:
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Leave Requests"
        ctx["status_choices"] = LeaveRequest.STATUS_CHOICES
        ctx["status_filter"] = self.request.GET.get("status", "")
        return ctx


# Roles that may file and decide leave for other people.
LEAVE_ADMINS = {"ADMIN", "HR_MANAGER"}


class LeaveRequestCreateView(LoginRequiredMixin, CreateView):
    model = LeaveRequest
    form_class = LeaveRequestForm
    template_name = "hr/leave_form.html"

    def get_success_url(self):
        from django.urls import reverse
        return reverse("hr:leave_list")

    def get_form_kwargs(self):
        from apps.core.guards import employee_of
        kwargs = super().get_form_kwargs()
        company = self.request.active_company
        admin = getattr(self.request, "company_role", None) in LEAVE_ADMINS or self.request.user.is_superuser
        kwargs.update(company=company, only_self=not admin, own_employee=employee_of(self.request.user, company))
        return kwargs

    def get_form(self, form_class=None):
        form = _style(super().get_form(form_class))
        if not form.is_bound:
            form.fields["start_date"].initial = datetime.date.today()
            form.fields["end_date"].initial = datetime.date.today()
        return form

    def form_valid(self, form):
        messages.success(self.request, "Leave request submitted.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "New Leave Request"
        return ctx


@login_required
def leave_approve(request, pk):
    """POST — approve or reject a pending leave request (not your own)."""
    from django.urls import reverse
    leave = get_scoped_or_404(LeaveRequest, request, lookup="employee__company", pk=pk)
    back = redirect(reverse("hr:leave_detail", args=[leave.pk]))
    if request.method != "POST":
        return back
    action = request.POST.get("action")
    if action not in ("approve", "reject"):
        messages.error(request, "Choose approve or reject.")
    elif leave.status != "PENDING":
        messages.error(request, f"This request is already {leave.get_status_display().lower()}.")
    elif leave.employee.user_id == request.user.pk:
        messages.error(request, "You can't decide your own leave. Another HR manager or an admin must do it.")
    elif action == "approve":
        leave.status = "APPROVED"
        leave.approved_by = request.user
        leave.approved_at = timezone.now()
        leave.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
        messages.success(request, f"Leave approved for {leave.employee.full_name}.")
    else:
        leave.status = "REJECTED"
        leave.approved_by = request.user
        leave.approved_at = timezone.now()
        leave.rejection_reason = request.POST.get("reason", "")
        leave.save(update_fields=["status", "approved_by", "approved_at", "rejection_reason", "updated_at"])
        messages.warning(request, f"Leave rejected for {leave.employee.full_name}.")
    return back


class HRIndexView(LoginRequiredMixin, TemplateView):
    template_name = "hr/index.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if company:
            from .models import Employee, LeaveRequest
            emps = Employee.objects.filter(company=company)
            ctx["employee_count"] = emps.filter(status="ACTIVE").count()
            ctx["total_headcount"] = emps.count()
            lr = LeaveRequest.objects.filter(employee__company=company)
            ctx["leaves_pending"] = lr.filter(status="PENDING").count()
            ctx["leaves_approved"] = lr.filter(status="APPROVED").count()
            ctx["recent_employees"] = emps.order_by("-created_at")[:6]
            ctx["recent_leaves"] = lr.select_related("employee").order_by("-created_at")[:6]
        return ctx


class LeaveRequestDetailView(CompanyScopedMixin, LoginRequiredMixin, DetailView):
    model = LeaveRequest
    company_lookup = "employee__company"
    template_name = "hr/leave_detail.html"
    context_object_name = "leave"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        role_ok = getattr(self.request, "company_role", None) in LEAVE_ADMINS or self.request.user.is_superuser
        ctx["can_decide"] = role_ok and self.object.employee.user_id != self.request.user.pk
        return ctx
