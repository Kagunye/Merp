"""Payroll views."""
import datetime
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.views.decorators.http import require_POST
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import CreateView, DetailView, ListView, TemplateView

from .models import PayrollRun, Payslip


class PayrollRunListView(LoginRequiredMixin, ListView):
    template_name = "payroll/run_list.html"
    context_object_name = "runs"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return PayrollRun.objects.none()
        return PayrollRun.objects.filter(company=company).order_by("-period_end")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Payroll Runs"
        return ctx


class PayrollRunCreateView(LoginRequiredMixin, CreateView):
    model = PayrollRun
    template_name = "payroll/run_form.html"
    fields = ["reference", "period_start", "period_end", "payment_date", "notes"]

    def get_success_url(self):
        from django.urls import reverse
        return reverse("payroll:run_detail", kwargs={"pk": self.object.pk})

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        from django import forms as dforms
        for fname, field in form.fields.items():
            if isinstance(field.widget, dforms.Textarea):
                field.widget.attrs["class"] = "form-textarea w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
                field.widget.attrs["rows"] = 3
            elif isinstance(field.widget, dforms.DateInput):
                field.widget.attrs["class"] = "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
                field.widget.attrs["type"] = "date"
            else:
                field.widget.attrs["class"] = "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
        company = self.request.active_company
        if company:
            from apps.core.models import SequenceCounter
            form.fields["reference"].initial = SequenceCounter.next_number(company, "PAY-")
        today = datetime.date.today()
        form.fields["period_start"].initial = today.replace(day=1)
        import calendar
        last_day = calendar.monthrange(today.year, today.month)[1]
        form.fields["period_end"].initial = today.replace(day=last_day)
        form.fields["payment_date"].initial = today.replace(day=last_day)
        return form

    def form_valid(self, form):
        from apps.core import guards
        from apps.hr.models import Employee
        from .statutory import compute
        company = self.request.active_company
        start, end = form.cleaned_data["period_start"], form.cleaned_data["period_end"]
        problems = []
        if end < start:
            form.add_error("period_end", "The period can't end before it starts.")
        try:
            guards.plausible_date(start, "Period start")
        except ValidationError as e:
            form.add_error("period_start", e.messages[0])
        if form.cleaned_data["payment_date"] < start:
            form.add_error("payment_date", "Pay day can't be before the period starts.")
        clash = PayrollRun.objects.filter(company=company, period_start__lte=end, period_end__gte=start) \
            .exclude(status="CANCELLED").first()
        if clash:
            form.add_error(None, f"{clash.reference} already covers {clash.period_start:%d %b} – "
                                 f"{clash.period_end:%d %b %Y}. Paying the same period twice would pay everyone twice.")
        if PayrollRun.objects.filter(company=company, reference__iexact=form.cleaned_data["reference"]).exists():
            form.add_error("reference", "That payroll reference is already used.")
        if form.errors:
            return self.form_invalid(form)

        employees = Employee.objects.filter(company=company, status__in=["ACTIVE", "PROBATION"], is_active=True,
                                            date_hired__lte=end)
        unpaid = []
        with transaction.atomic():
            form.instance.company = company
            form.instance.prepared_by = self.request.user
            run = self.object = form.save()
            totals = {"gross": Decimal("0"), "ded": Decimal("0"), "net": Decimal("0")}
            for emp in employees:
                contract = emp.contracts.filter(is_current=True).first()
                if not contract or contract.basic_salary <= 0:
                    unpaid.append(emp.full_name)
                    continue
                p = compute(contract.basic_salary)
                Payslip.objects.create(
                    payroll_run=run, employee=emp, gross_salary=p.gross, total_allowances=Decimal("0"),
                    total_deductions=p.deductions, paye_tax=p.paye, nssf_employee=p.nssf, nhif_shif=p.shif,
                    housing_levy=p.housing_levy, net_salary=p.net,
                )
                totals["gross"] += p.gross
                totals["ded"] += p.deductions
                totals["net"] += p.net
            run.total_gross, run.total_deductions, run.total_net = totals["gross"], totals["ded"], totals["net"]
            run.save(update_fields=["total_gross", "total_deductions", "total_net"])
        messages.success(self.request, f"Payroll run '{run.reference}' prepared with {run.payslips.count()} payslips. "
                                       f"A finance manager must approve it before it is posted.")
        if unpaid:
            messages.warning(self.request, f"Left out — no salary on record: {', '.join(unpaid[:10])}"
                                           f"{'…' if len(unpaid) > 10 else ''}. Add their contract in HR, then run "
                                           f"a supplementary payroll.")
        return redirect(self.get_success_url())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "New Payroll Run"
        return ctx


class PayrollRunDetailView(LoginRequiredMixin, DetailView):
    model = PayrollRun
    template_name = "payroll/run_detail.html"
    context_object_name = "run"

    def get_queryset(self):
        return PayrollRun.objects.filter(company=self.request.active_company)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Payroll: {self.object.reference}"
        ctx["payslips"] = self.object.payslips.select_related("employee").order_by("employee__last_name")
        return ctx


class PayslipDetailView(LoginRequiredMixin, DetailView):
    model = Payslip
    template_name = "payroll/payslip_detail.html"
    context_object_name = "payslip"

    def get_queryset(self):
        return Payslip.objects.filter(payroll_run__company=self.request.active_company).select_related("payroll_run", "employee")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ps = self.object
        ctx["page_title"] = f"Payslip — {ps.employee.full_name}"
        return ctx


class PayrollIndexView(LoginRequiredMixin, TemplateView):
    template_name = "payroll/index.html"

    def get_context_data(self, **kwargs):
        from decimal import Decimal
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if not company:
            return ctx
        runs = PayrollRun.objects.filter(company=company)
        ctx["run_count"] = runs.count()
        ctx["last_run"] = runs.order_by("-period_end").first()
        ctx["draft_count"] = runs.filter(status="DRAFT").count()
        ctx["approved_count"] = runs.filter(status="APPROVED").count()
        ctx["recent_runs"] = runs.order_by("-period_end")[:8]
        return ctx


class PayslipListView(LoginRequiredMixin, ListView):
    model = Payslip
    template_name = "payroll/payslip_list.html"
    context_object_name = "payslips"
    paginate_by = 50

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Payslip.objects.none()
        return Payslip.objects.filter(payroll_run__company=company).select_related("employee", "payroll_run")


@login_required
@require_POST
def run_approve(request, pk):
    """A second person approves the run; approval posts it to the ledger."""
    run = get_object_or_404(PayrollRun, pk=pk, company=request.active_company)
    if run.status != "DRAFT":
        messages.error(request, f"{run.reference} is {run.get_status_display().lower()}.")
    elif run.prepared_by_id == request.user.pk:
        messages.error(request, f"You prepared {run.reference}; someone else must approve it.")
    elif not run.payslips.exists():
        messages.error(request, f"{run.reference} has no payslips to approve.")
    else:
        from django.utils import timezone
        from apps.accounting.gl_service import post_payroll
        try:
            with transaction.atomic():
                post_payroll(run, user=request.user)
                PayrollRun.objects.filter(pk=run.pk).update(status="POSTED", approved_by=request.user,
                                                            approved_at=timezone.now())
        except (ValidationError, ValueError) as e:
            messages.error(request, " ".join(getattr(e, "messages", [str(e)])))
        else:
            messages.success(request, f"{run.reference} approved and posted: net pay {run.total_net:,.2f} due to staff.")
    return redirect("payroll:run_detail", pk=pk)
