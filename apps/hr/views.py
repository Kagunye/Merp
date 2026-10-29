"""HR views."""
import datetime
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import CreateView, DetailView, ListView, TemplateView, UpdateView

from apps.core.mixins import CompanyRequiredMixin
from .forms import ContractForm, EmployeeForm
from .models import Contract, Employee, LeaveRequest, LeaveType
from apps.core.forms import ISO_DATE


def _style(form):
    """One look for every field, without repeating the class list per view."""
    from django import forms as dforms
    for field in form.fields.values():
        if isinstance(field.widget, dforms.Select):
            field.widget.attrs["class"] = (
                "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm")
        elif isinstance(field.widget, dforms.Textarea):
            field.widget.attrs["class"] = (
                "form-textarea w-full border border-gray-300 rounded-lg px-3 py-2 text-sm")
            field.widget.attrs["rows"] = 3
        elif isinstance(field.widget, dforms.DateInput):
            field.widget.attrs["class"] = (
                "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm")
            field.widget.attrs["type"] = "date"
            field.widget.format = ISO_DATE
        else:
            field.widget.attrs["class"] = (
                "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm")


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


class EmployeeCreateView(LoginRequiredMixin, CreateView):
    model = Employee
    template_name = "hr/employee_form.html"
    form_class = EmployeeForm

    def get_success_url(self):
        from django.urls import reverse
        return reverse("hr:employee_detail", kwargs={"pk": self.object.pk})

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["date_hired"].initial = datetime.date.today()
        return form

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        messages.success(self.request, f"Employee '{form.instance.full_name}' created.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "New Employee"
        return ctx


class EmployeeDetailView(LoginRequiredMixin, DetailView):
    model = Employee
    template_name = "hr/employee_detail.html"
    context_object_name = "employee"

    def get_queryset(self):
        return Employee.objects.filter(company=self.request.active_company)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = self.object.full_name
        ctx["contracts"] = self.object.contracts.order_by("-start_date")[:5]
        ctx["leave_requests"] = self.object.leave_requests.order_by("-created_at")[:10]
        ctx["payslips"] = self.object.payslips.select_related("payroll_run").order_by("-created_at")[:10]
        return ctx


class ContractCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    """Write the pay a person is actually on.

    This is the only screen where a salary figure is entered, so payroll has
    nowhere to come from until one exists — which is why an employee can be
    reached straight into it.
    """
    model = Contract
    form_class = ContractForm
    template_name = "hr/contract_form.html"

    @property
    def employee(self):
        """Looked up lazily, so the company guard runs before this page asks."""
        if not hasattr(self, "_employee"):
            self._employee = get_object_or_404(
                Employee, pk=self.kwargs["employee_pk"],
                company=self.request.active_company)
        return self._employee

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        initial.update({
            "employee": self.employee.pk,
            "department": self.employee.department_id,
            "job_title": self.employee.job_title,
            "start_date": self.employee.date_hired,
        })
        return initial

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        _style(ctx["form"])
        ctx["page_title"] = "New Contract"
        ctx["employee"] = self.employee
        return ctx

    def form_valid(self, form):
        messages.success(
            self.request,
            f"{form.instance.employee.full_name}'s contract is saved"
            + (" — it is the pay payroll works from."
               if form.instance.is_current else
               ", but it is not marked current, so payroll will not use it."))
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("hr:employee_detail", kwargs={"pk": self.object.employee_id})


class ContractUpdateView(LoginRequiredMixin, UpdateView):
    model = Contract
    form_class = ContractForm
    template_name = "hr/contract_form.html"

    def get_queryset(self):
        return Contract.objects.filter(
            employee__company=self.request.active_company).select_related("employee")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        _style(ctx["form"])
        ctx["page_title"] = f"Contract — {self.object.employee.full_name}"
        ctx["employee"] = self.object.employee
        return ctx

    def form_valid(self, form):
        messages.success(
            self.request,
            f"{form.instance.employee.full_name}'s contract now pays "
            f"{form.instance.basic_salary} basic.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("hr:employee_detail", kwargs={"pk": self.object.employee_id})


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


class LeaveRequestCreateView(LoginRequiredMixin, CreateView):
    model = LeaveRequest
    template_name = "hr/leave_form.html"
    fields = ["employee", "leave_type", "start_date", "end_date", "notes"]

    def get_success_url(self):
        from django.urls import reverse
        return reverse("hr:leave_list")

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        company = self.request.active_company
        from django import forms as dforms
        for fname, field in form.fields.items():
            if isinstance(field.widget, dforms.Select):
                field.widget.attrs["class"] = "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
            elif isinstance(field.widget, dforms.Textarea):
                field.widget.attrs["class"] = "form-textarea w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
                field.widget.attrs["rows"] = 3
            elif isinstance(field.widget, dforms.DateInput):
                field.widget.attrs["class"] = "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
                field.widget.attrs["type"] = "date"
                field.widget.format = ISO_DATE
            else:
                field.widget.attrs["class"] = "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
        if company:
            form.fields["employee"].queryset = Employee.objects.filter(company=company, is_active=True, status="ACTIVE")
            form.fields["leave_type"].queryset = LeaveType.objects.filter(company=company, is_active=True)
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


def leave_approve(request, pk):
    """POST — approve or reject a leave request."""
    from django.contrib.auth.decorators import login_required
    leave = get_object_or_404(LeaveRequest, pk=pk, employee__company=request.active_company)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "approve":
            leave.status = "APPROVED"
            leave.approved_by = request.user
            leave.approved_at = timezone.now()
            leave.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
            messages.success(request, f"Leave approved for {leave.employee.full_name}.")
        elif action == "reject":
            leave.status = "REJECTED"
            leave.rejection_reason = request.POST.get("reason", "")
            leave.save(update_fields=["status", "rejection_reason", "updated_at"])
            messages.warning(request, f"Leave rejected for {leave.employee.full_name}.")
    from django.urls import reverse
    return redirect(reverse("hr:leave_list"))


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


class LeaveRequestDetailView(LoginRequiredMixin, DetailView):
    model = LeaveRequest
    template_name = "hr/leave_detail.html"
    context_object_name = "leave"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return LeaveRequest.objects.none()
        return LeaveRequest.objects.filter(employee__company=company)

