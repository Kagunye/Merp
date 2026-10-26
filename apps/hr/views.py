"""HR views."""
import datetime
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import CreateView, DetailView, ListView, TemplateView, UpdateView

from apps.core.mixins import CompanyRequiredMixin
from .access import manages_people, own_employee
from .forms import (
    LEAVER_STATUSES, ContractForm, EmployeeForm, LeaveRequestForm, TerminationForm,
)
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
    fields = [
        "employee_number", "first_name", "last_name", "other_names",
        "gender", "date_of_birth", "date_hired", "status",
        "department", "branch", "job_title", "reports_to",
        "national_id", "kra_pin", "nssf_number", "nhif_number",
        "phone", "email", "bank_name", "bank_account_number", "notes",
    ]

    def get_success_url(self):
        from django.urls import reverse
        return reverse("hr:employee_detail", kwargs={"pk": self.object.pk})

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        company = self.request.active_company
        # Style all fields
        for fname, field in form.fields.items():
            from django import forms as dforms
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
            from apps.organizations.models import Department, Branch
            form.fields["department"].queryset = Department.objects.filter(company=company, is_active=True)
            form.fields["branch"].queryset = Branch.objects.filter(company=company, is_active=True)
            form.fields["reports_to"].queryset = Employee.objects.filter(company=company, is_active=True)
            from apps.core.models import SequenceCounter
            # A preview: the number is taken at save time (claim_number), so
            # opening this form and leaving does not leave a gap in the series.
            form.fields["employee_number"].initial = SequenceCounter.peek_number(company, "EMP-")
        form.fields["date_hired"].initial = datetime.date.today()
        return form

    def form_valid(self, form):
        from apps.core.models import SequenceCounter
        company = self.request.active_company
        form.instance.company = company
        form.instance.employee_number = SequenceCounter.claim_number(
            company, "EMP-", form.instance.employee_number,
            taken=lambda ref: Employee.objects.filter(company=company, employee_number=ref).exists())
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


class EmployeeUpdateView(LoginRequiredMixin, CompanyRequiredMixin, UpdateView):
    """Correct an employee's record, and link it to the login they use."""
    model = Employee
    form_class = EmployeeForm
    template_name = "hr/employee_form.html"

    def get_queryset(self):
        return Employee.objects.filter(company=self.request.active_company)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        _style(form)
        return form

    def form_valid(self, form):
        was_leaver = form.initial.get("status") in LEAVER_STATUSES
        employee = form.save(commit=False)
        if was_leaver and employee.status not in LEAVER_STATUSES:
            # Taken back on: the old leaving date no longer stops their pay,
            # but the contract it ended is still history. Pay restarts from a
            # new contract, so say so rather than let the next run skip them.
            employee.date_terminated = None
            messages.warning(
                self.request,
                f"{employee.full_name} is back on the staff list. Their old contract "
                "ended when they left — add a new contract from the day they restart, "
                "or payroll has nothing to pay them from.")
        employee.save()
        messages.success(self.request, f"{employee.full_name}'s record is saved.")
        return redirect("hr:employee_detail", pk=employee.pk)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit — {self.object.full_name}"
        return ctx


class EmployeeTerminateView(LoginRequiredMixin, CompanyRequiredMixin, TemplateView):
    """Record somebody leaving: status, last day, and the end of their contract.

    Payroll pays them up to and including the last day, pro-rated, and nothing
    for any day after it.
    """
    template_name = "hr/employee_terminate.html"

    @property
    def employee(self):
        if not hasattr(self, "_employee"):
            self._employee = get_object_or_404(
                Employee, pk=self.kwargs["pk"], company=self.request.active_company)
        return self._employee

    def get_form(self):
        data = self.request.POST if self.request.method == "POST" else None
        form = TerminationForm(
            data, employee=self.employee,
            initial={"date_terminated": self.employee.date_terminated
                     or timezone.localdate()})
        _style(form)
        return form

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.setdefault("form", self.get_form())
        ctx["employee"] = self.employee
        ctx["page_title"] = f"Terminate — {self.employee.full_name}"
        return ctx

    def post(self, request, *args, **kwargs):
        from .services import terminate_employee

        form = self.get_form()
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        day = form.cleaned_data["date_terminated"]
        warnings = terminate_employee(self.employee, day, form.cleaned_data["status"])
        messages.success(
            request,
            f"{self.employee.full_name} leaves on {day:%d %b %Y}. Their contract ends "
            "that day: payroll pays them up to it, pro-rated, and not after it.")
        for warning in warnings:
            messages.warning(request, warning)
        return redirect("hr:employee_detail", pk=self.employee.pk)


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
            + (" — it is the pay in force today."
               if form.instance.is_current else
               " as history: payroll pays it for the days its dates cover, but it is "
               "not the contract in force today."))
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


def visible_leave(request):
    """The leave requests this login may see in the active company.

    Whoever runs the staff sees everybody's; anybody else sees their own and
    nothing more. A login with no employee record has no leave of its own, so
    it sees none — falling through to everybody's would show a sales clerk the
    whole company's sick notes.
    """
    company = getattr(request, "active_company", None)
    if not company:
        return LeaveRequest.objects.none()
    qs = LeaveRequest.objects.filter(employee__company=company)
    if manages_people(request.user, company):
        return qs
    mine = own_employee(request.user, company)
    return qs.filter(employee=mine) if mine else LeaveRequest.objects.none()


class LeaveRequestListView(LoginRequiredMixin, ListView):
    template_name = "hr/leave_list.html"
    context_object_name = "leaves"
    paginate_by = 25

    def get_queryset(self):
        qs = visible_leave(self.request).select_related("employee", "leave_type")
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


class LeaveRequestCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    model = LeaveRequest
    template_name = "hr/leave_form.html"
    form_class = LeaveRequestForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        kwargs["user"] = self.request.user
        return kwargs

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
    """POST — approve or reject a leave request.

    Deciding leave is the staff side's job, and never the job of the person
    who is going on it: days granted to yourself are days nobody else agreed
    to cover.
    """
    company = request.active_company
    leave = get_object_or_404(visible_leave(request), pk=pk)
    if request.method == "POST":
        action = request.POST.get("action")
        if action in ("approve", "reject") and not manages_people(request.user, company):
            messages.error(
                request,
                "Only HR or an administrator can approve or reject leave. Ask them to "
                "look at this request.")
            return redirect("hr:leave_list")
        if action in ("approve", "reject") and leave.employee.user_id == request.user.pk:
            messages.error(
                request,
                "You cannot decide your own leave request — somebody else has to "
                "approve or reject it.")
            return redirect("hr:leave_list")
        if action in ("approve", "reject") and leave.status != "PENDING":
            messages.error(
                request,
                f"This request is already {leave.get_status_display().lower()}, so there "
                "is nothing left to decide.")
            return redirect("hr:leave_list")
        if action == "approve":
            leave.status = "APPROVED"
            leave.approved_by = request.user
            leave.approved_at = timezone.now()
            leave.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
            messages.success(request, f"Leave approved for {leave.employee.full_name}.")
        elif action == "reject":
            leave.status = "REJECTED"
            leave.approved_by = request.user
            leave.approved_at = timezone.now()
            leave.rejection_reason = request.POST.get("reason", "")
            leave.save(update_fields=["status", "approved_by", "approved_at", "rejection_reason", "updated_at"])
            messages.warning(request, f"Leave rejected for {leave.employee.full_name}.")
        else:
            messages.error(
                request, "Choose whether to approve or reject the request.")
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
            # The list of names is the same privacy as the leave list itself.
            ctx["recent_leaves"] = visible_leave(self.request).select_related(
                "employee").order_by("-created_at")[:6]
        return ctx


class LeaveRequestDetailView(LoginRequiredMixin, DetailView):
    model = LeaveRequest
    template_name = "hr/leave_detail.html"
    context_object_name = "leave"

    def get_queryset(self):
        return visible_leave(self.request)

    def get_context_data(self, **kwargs):
        from apps.core import access
        ctx = super().get_context_data(**kwargs)
        role = getattr(self.request, "company_role", None)
        ctx["can_decide"] = (access.can(self.request.user, role, "hr", "leave_approve", "POST")
                             and self.object.employee.user_id != self.request.user.pk)
        return ctx

