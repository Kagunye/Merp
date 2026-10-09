"""Payroll views."""
import datetime
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import CreateView, DetailView, ListView

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
        company = self.request.active_company
        form.instance.company = company
        with transaction.atomic():
            run = form.save()
            # Generate payslips for all active employees
            from apps.hr.models import Employee
            employees = Employee.objects.filter(company=company, status__in=["ACTIVE", "PROBATION"], is_active=True)
            total_gross = Decimal("0")
            total_deductions = Decimal("0")
            total_net = Decimal("0")
            for emp in employees:
                # Get basic salary from current contract
                contract = emp.contracts.filter(is_current=True).first()
                basic = contract.basic_salary if contract else Decimal("0")
                # Simple Kenya PAYE calculation
                paye = Decimal("0")
                nssf = min(Decimal("2160"), basic * Decimal("0.06"))
                nhif = Decimal("500")
                housing = basic * Decimal("0.015")
                gross = basic
                deductions = paye + nssf + nhif + housing
                net = gross - deductions
                total_gross += gross
                total_deductions += deductions
                total_net += net
                Payslip.objects.create(
                    payroll_run=run,
                    employee=emp,
                    gross_salary=gross,
                    total_allowances=Decimal("0"),
                    total_deductions=deductions,
                    paye_tax=paye,
                    nssf_employee=nssf,
                    nhif_shif=nhif,
                    housing_levy=housing,
                    net_salary=net,
                )
            run.total_gross = total_gross
            run.total_deductions = total_deductions
            run.total_net = total_net
            run.save(update_fields=["total_gross", "total_deductions", "total_net"])
        messages.success(self.request, f"Payroll run '{run.reference}' created with {employees.count()} payslips.")
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
