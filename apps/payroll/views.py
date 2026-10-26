"""Payroll views.

A run moves through four rooms and no view lets it skip one: it is calculated
from the tables, looked at, approved by somebody, and only then posted to the
ledger as a single journal entry. After that it is history — put right by
reversal, never by editing.
"""
import calendar
import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, TemplateView, UpdateView

from apps.accounting.currency import book_currency
from apps.core.mixins import CompanyRequiredMixin
from . import services
from .forms import PayrollRunForm, SalaryComponentForm, StatutoryRuleForm
from .models import (
    SETTLED_RUNS, PayrollRun, PayrollTaxBand, Payslip, SalaryComponent,
    StatutoryRule,
)


def _form_class(form):
    """One look for every field in this app, without repeating the class list."""
    from django import forms as dforms
    for field in form.fields.values():
        if isinstance(field.widget, dforms.Textarea):
            field.widget.attrs["class"] = (
                "form-textarea w-full border border-gray-300 rounded-lg px-3 py-2 text-sm")
        else:
            field.widget.attrs["class"] = (
                "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm")


def describe_calculation(request, run):
    """Run the tables and say what came out — including who was left off.

    A run that quietly skips an employee is how somebody goes a month unpaid, so
    every name the contract check turned away is said out loud.
    """
    try:
        result = services.calculate(run)
    except ValidationError as exc:
        messages.error(
            request,
            f"'{run.reference}' is saved as a draft, but nothing could be calculated: "
            f"{exc.messages[0]}")
        return None
    if result["count"]:
        messages.success(
            request,
            f"{run.reference}: {result['count']} payslip(s) worked out from the table "
            f"— {run.total_gross} gross, {run.total_deductions} withheld, "
            f"{run.total_net} to pay.")
    else:
        messages.warning(
            request,
            f"{run.reference} has no payslips: nobody on the staff list has a contract "
            "running across this period.")
    for name in result["unpaid"]:
        messages.warning(
            request,
            f"{name} is on the staff list but has no contract covering "
            f"{run.period_start:%d %b} to {run.period_end:%d %b %Y}, so they are not on "
            "this run.")
    return result


class PayrollIndexView(LoginRequiredMixin, TemplateView):
    template_name = "payroll/index.html"

    def get_context_data(self, **kwargs):
        from apps.hr.models import Employee

        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        ctx["page_title"] = "Payroll"
        if not company:
            return ctx
        runs = PayrollRun.objects.filter(company=company)
        ctx["run_count"] = runs.count()
        ctx["last_run"] = runs.order_by("-period_end").first()
        ctx["draft_count"] = runs.filter(status__in=["DRAFT", "REVIEW"]).count()
        ctx["approved_count"] = runs.filter(status="APPROVED").count()
        ctx["recent_runs"] = runs.order_by("-period_end")[:8]
        staff = Employee.objects.filter(
            company=company, status__in=["ACTIVE", "PROBATION"],
            is_active=True).prefetch_related("contracts")
        ctx["staff_count"] = staff.count()
        # Measured against today: a lapsed contract is the same hole in next
        # month's run as it is in this one.
        today = datetime.date.today()
        ctx["unpaid_staff"] = sum(
            1 for e in staff if services.current_contract(e, today, today) is None)
        ctx["rule_count"] = len(services.rules_as_at(company, datetime.date.today()))
        ctx["component_count"] = SalaryComponent.objects.filter(
            company=company, is_active=True).count()
        return ctx


class PayrollRunListView(LoginRequiredMixin, ListView):
    template_name = "payroll/run_list.html"
    context_object_name = "runs"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return PayrollRun.objects.none()
        qs = PayrollRun.objects.filter(company=company).select_related("journal_entry")
        status = self.request.GET.get("status")
        if status in dict(PayrollRun.STATUS_CHOICES):
            qs = qs.filter(status=status)
        return qs.order_by("-period_end")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Payroll Runs"
        ctx["statuses"] = PayrollRun.STATUS_CHOICES
        ctx["selected_status"] = self.request.GET.get("status", "")
        ctx["book"] = book_currency(self.request.active_company)
        return ctx


class PayrollRunCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    model = PayrollRun
    form_class = PayrollRunForm
    template_name = "payroll/run_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_initial(self):
        today = datetime.date.today()
        last_day = calendar.monthrange(today.year, today.month)[1]
        return {
            "period_start": today.replace(day=1),
            "period_end": today.replace(day=last_day),
            "payment_date": today.replace(day=last_day),
        }

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        run = form.save()
        describe_calculation(self.request, run)
        return redirect("payroll:run_detail", pk=run.pk)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        _form_class(ctx["form"])
        ctx["page_title"] = "New Payroll Run"
        ctx["action"] = "Create and calculate"
        return ctx


class PayrollRunUpdateView(LoginRequiredMixin, UpdateView):
    model = PayrollRun
    form_class = PayrollRunForm
    template_name = "payroll/run_form.html"

    def get_queryset(self):
        return PayrollRun.objects.filter(company=self.request.active_company)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def dispatch(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self.object.is_editable:
            messages.error(
                request,
                f"'{self.object.reference}' is {self.object.status.lower()} — only a "
                "draft can be changed. Reverse it and raise a fresh run instead.")
            return redirect("payroll:run_detail", pk=self.object.pk)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        run = form.save()
        describe_calculation(self.request, run)
        return redirect("payroll:run_detail", pk=run.pk)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        _form_class(ctx["form"])
        ctx["page_title"] = f"Amend: {self.object.reference}"
        ctx["action"] = "Save and recalculate"
        return ctx


class PayrollRunDetailView(LoginRequiredMixin, DetailView):
    model = PayrollRun
    template_name = "payroll/run_detail.html"
    context_object_name = "run"

    def get_queryset(self):
        return PayrollRun.objects.filter(
            company=self.request.active_company).select_related("journal_entry")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        run = self.object
        ctx["page_title"] = f"Payroll: {run.reference}"
        ctx["book"] = book_currency(run.company)
        ctx["payslips"] = run.payslips.select_related("employee").prefetch_related(
            "lines__gl_account").order_by("employee__last_name", "employee__first_name")
        ctx["lines"] = {slip.pk: list(slip.lines.all()) for slip in ctx["payslips"]}
        ctx["rules"] = services.rules_as_at(run.company, run.period_end)
        ctx["can_edit"] = run.is_editable
        if run.journal_entry_id:
            ctx["entry"] = run.journal_entry
        return ctx


@login_required
@require_POST
def run_calculate(request, pk):
    run = get_object_or_404(PayrollRun, pk=pk, company=request.active_company)
    describe_calculation(request, run)
    return redirect("payroll:run_detail", pk=run.pk)


@login_required
@require_POST
def run_approve(request, pk):
    run = get_object_or_404(PayrollRun, pk=pk, company=request.active_company)
    if run.status == "APPROVED":
        messages.warning(request, f"'{run.reference}' is already approved.")
        return redirect("payroll:run_detail", pk=run.pk)
    if run.status not in ("DRAFT", "REVIEW"):
        messages.error(
            request,
            f"'{run.reference}' is {run.status.lower()} and cannot be approved from "
            "there — a posted run is in the ledger and a cancelled one is finished.")
        return redirect("payroll:run_detail", pk=run.pk)
    clash = services.overlapping_run(run.company, run.period_start, run.period_end, exclude=run)
    if clash:
        messages.error(
            request,
            f"'{clash.reference}' already pays {clash.period_start} to "
            f"{clash.period_end}. Cancel one of the two runs first, or everybody is "
            "paid twice.")
        return redirect("payroll:run_detail", pk=run.pk)
    if not run.payslips.exists():
        messages.error(
            request,
            "There is nothing to approve: this run has no payslips. Recalculate it, or "
            "put the missing contracts in first.")
        return redirect("payroll:run_detail", pk=run.pk)
    run.status = "APPROVED"
    run.approved_by = request.user
    run.approved_at = timezone.now()
    run.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    messages.success(
        request,
        f"'{run.reference}' approved for {run.payslips.count()} employee(s) — "
        f"{run.total_net} to pay on {run.payment_date:%d %b %Y}.")
    return redirect("payroll:run_detail", pk=run.pk)


@login_required
@require_POST
def run_post(request, pk):
    from apps.accounting import gl_service

    get_object_or_404(PayrollRun, pk=pk, company=request.active_company)
    try:
        with transaction.atomic():
            # Two clicks on Post must not book the month twice: the second one
            # waits here for the first, then finds the entry already made.
            run = PayrollRun.objects.select_for_update().get(pk=pk)
            if run.journal_entry_id:
                messages.warning(request, f"'{run.reference}' is already in the ledger.")
                return redirect("payroll:run_detail", pk=run.pk)
            if run.status != "APPROVED":
                messages.error(
                    request,
                    f"'{run.reference}' is {run.status.lower()}, and pay does not go into "
                    "the book until it has been approved. Approve it first.")
                return redirect("payroll:run_detail", pk=run.pk)
            clash = services.overlapping_run(run.company, run.period_start, run.period_end, exclude=run)
            if clash:
                messages.error(
                    request,
                    f"'{clash.reference}' already pays {clash.period_start} to "
                    f"{clash.period_end}. Cancel one of the two runs before either is "
                    "posted, or everybody is paid twice.")
                return redirect("payroll:run_detail", pk=run.pk)
            entry = gl_service.post_payroll_run(run, user=request.user)
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect("payroll:run_detail", pk=run.pk)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("payroll:run_detail", pk=run.pk)
    messages.success(
        request,
        f"{entry.reference} posted: {run.total_net} paid out, "
        f"{run.total_deductions} withheld for the authorities, "
        f"{run.total_employer} added as employer cost.")
    if entry.calendar_warning:
        messages.warning(request, entry.calendar_warning)
    return redirect("payroll:run_detail", pk=run.pk)


@login_required
@require_POST
def run_cancel(request, pk):
    """Stand a run down. A posted one is corrected by an offsetting entry, in the
    period the pay belongs to — never by deleting what the ledger already says."""
    run = get_object_or_404(PayrollRun, pk=pk, company=request.active_company)
    if run.status == "CANCELLED":
        messages.warning(request, f"'{run.reference}' is already cancelled.")
        return redirect("payroll:run_detail", pk=run.pk)
    reason = (request.POST.get("reason") or "").strip()
    with transaction.atomic():
        entry = run.journal_entry
        reversal = None
        if entry and entry.status == "POSTED":
            reversal = entry.reverse(user=request.user)
            if reason:
                reversal.notes = f"{reversal.notes} — {reason}".strip(" —")
                reversal.save(update_fields=["notes", "updated_at"])
        run.status = "CANCELLED"
        run.save(update_fields=["status", "updated_at"])
    if reversal:
        messages.success(
            request,
            f"'{run.reference}' cancelled: {entry.reference} reversed through "
            f"{reversal.reference}, so the ledger no longer owes this month's pay.")
    else:
        messages.success(
            request,
            f"'{run.reference}' cancelled. Nothing was in the ledger, so there was "
            "nothing to reverse.")
    return redirect("payroll:run_detail", pk=run.pk)


class PayslipListView(LoginRequiredMixin, ListView):
    model = Payslip
    template_name = "payroll/payslip_list.html"
    context_object_name = "payslips"
    paginate_by = 50

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Payslip.objects.none()
        qs = Payslip.objects.filter(payroll_run__company=company).select_related(
            "employee", "payroll_run")
        run = self.request.GET.get("run")
        if run:
            qs = qs.filter(payroll_run_id=run)
        return qs.order_by("employee__last_name", "employee__first_name")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        ctx["page_title"] = "Payslips"
        ctx["runs"] = PayrollRun.objects.filter(company=company).order_by("-period_end")
        ctx["selected_run"] = self.request.GET.get("run", "")
        ctx["book"] = book_currency(company)
        return ctx


class PayslipDetailView(LoginRequiredMixin, DetailView):
    model = Payslip
    template_name = "payroll/payslip_detail.html"
    context_object_name = "payslip"

    def get_queryset(self):
        return Payslip.objects.filter(
            payroll_run__company=self.request.active_company).select_related(
            "payroll_run", "payroll_run__company", "employee")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        slip = self.object
        ctx["page_title"] = f"Payslip — {slip.employee.full_name}"
        ctx["earnings"] = slip.lines.filter(kind="EARNING")
        ctx["deductions"] = slip.lines.filter(kind="DEDUCTION")
        ctx["employer"] = slip.lines.filter(kind="EMPLOYER")
        ctx["book"] = book_currency(slip.payroll_run.company)
        return ctx


class StatutoryRuleListView(LoginRequiredMixin, ListView):
    """The rates table: the row in force, and every older version beside it."""
    template_name = "payroll/rule_list.html"
    context_object_name = "rules"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return StatutoryRule.objects.none()
        return StatutoryRule.objects.filter(company=company).select_related(
            "payable_account", "expense_account").prefetch_related("tax_bands")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        ctx["page_title"] = "Statutory Tables"
        today = datetime.date.today()
        in_force = {r.pk for r in services.rules_as_at(company, today)}
        rows = list(ctx["rules"])
        totals = {}
        for rule in rows:
            totals[rule.code] = totals.get(rule.code, 0) + 1
        # The ordering is by code, newest first, so the versions of one code are
        # already beside each other — say which row actually applies today.
        for rule in rows:
            rule.total_versions = totals[rule.code]
            rule.is_in_force = rule.pk in in_force
        ctx["rules"] = rows
        ctx["tiered"] = any(
            r.method == "BANDS" and r.is_active and r.pk in in_force for r in rows)
        return ctx


@login_required
@require_POST
def rules_seed(request):
    """Load the standard table into a book that has none — for companies founded
    before payroll grew one."""
    company = request.active_company
    if not company:
        messages.error(request, "Choose a company first.")
        return redirect("payroll:rule_list")
    created = services.ensure_default_table(company, datetime.date.today().replace(day=1))
    if created:
        messages.success(
            request, "Standard table loaded: " + ", ".join(created) + ".")
    else:
        messages.info(request, "Every code in the standard table already has a rule here.")
    return redirect("payroll:rule_list")


class StatutoryRuleCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    model = StatutoryRule
    form_class = StatutoryRuleForm
    template_name = "payroll/rule_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        rule = form.save()
        messages.success(
            self.request,
            f"{rule.code} is in force from {rule.effective_from:%d %b %Y}.")
        if rule.method == "BANDS":
            messages.info(
                self.request,
                "A tiered table pays nothing until it has steps — add the bands below.")
        return redirect("payroll:rule_detail", pk=rule.pk)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        _form_class(ctx["form"])
        ctx["page_title"] = "New Statutory Table"
        ctx["action"] = "Create"
        return ctx


class StatutoryRuleUpdateView(LoginRequiredMixin, UpdateView):
    """A rate is corrected by a new row with a new start date. Editing a table
    that has already paid people rewrites the arithmetic on their payslips."""
    model = StatutoryRule
    form_class = StatutoryRuleForm
    template_name = "payroll/rule_form.html"

    def get_queryset(self):
        return StatutoryRule.objects.filter(
            company=self.request.active_company).exclude(
            payslip_lines__payslip__payroll_run__status__in=SETTLED_RUNS)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def form_valid(self, form):
        self.object = form.save()
        messages.success(self.request, f"{self.object.code} updated.")
        return redirect("payroll:rule_detail", pk=self.object.pk)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        _form_class(ctx["form"])
        ctx["page_title"] = f"Edit: {self.object.code}"
        ctx["action"] = "Save changes"
        return ctx


class StatutoryRuleDetailView(LoginRequiredMixin, DetailView):
    model = StatutoryRule
    template_name = "payroll/rule_detail.html"
    context_object_name = "rule"

    def get_queryset(self):
        return StatutoryRule.objects.filter(
            company=self.request.active_company).select_related(
            "payable_account", "expense_account")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"{self.object.code} — {self.object.name}"
        ctx["bands"] = self.object.bands()
        # The same test the edit view's queryset applies: a table that has already
        # paid a run somebody signed off is history, not a draft.
        ctx["can_edit"] = StatutoryRule.objects.filter(pk=self.object.pk).exclude(
            payslip_lines__payslip__payroll_run__status__in=SETTLED_RUNS,
        ).exists()
        ctx["history"] = StatutoryRule.objects.filter(
            company=self.object.company, code=self.object.code,
        ).order_by("-effective_from")
        return ctx


@login_required
@require_POST
def rule_deactivate(request, pk):
    rule = get_object_or_404(StatutoryRule, pk=pk, company=request.active_company)
    rule.is_active = not rule.is_active
    rule.save(update_fields=["is_active", "updated_at"])
    messages.success(
        request,
        f"{rule.code} ({rule.effective_from:%d %b %Y}) is now "
        f"{'active again' if rule.is_active else 'retired'}.")
    return redirect("payroll:rule_detail", pk=rule.pk)


class BandCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    """One step of a tiered table, added to a rule that is already saved."""
    model = PayrollTaxBand
    fields = ["lower", "upper", "percent"]
    template_name = "payroll/band_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.rule = get_object_or_404(
            StatutoryRule, pk=kwargs["rule_pk"], company=request.active_company)
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # Attached before validation, not after: the band checks itself against
        # the steps its table already has, and it cannot do that while it belongs
        # to no rule at all.
        form.instance.rule = self.rule
        return form

    def form_valid(self, form):
        self.object = form.save()
        messages.success(self.request, f"Band '{self.object}' added to {self.rule.code}.")
        return redirect("payroll:rule_detail", pk=self.rule.pk)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        _form_class(ctx["form"])
        ctx["page_title"] = f"New band for {self.rule.code}"
        ctx["rule"] = self.rule
        ctx["action"] = "Add band"
        return ctx


@login_required
@require_POST
def band_delete(request, pk):
    band = get_object_or_404(
        PayrollTaxBand, rule__company=request.active_company, pk=pk)
    rule = band.rule
    band.delete()
    messages.success(request, f"Band removed from {rule.code}.")
    return redirect("payroll:rule_detail", pk=rule.pk)


class ComponentListView(LoginRequiredMixin, ListView):
    template_name = "payroll/component_list.html"
    context_object_name = "components"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return SalaryComponent.objects.none()
        return SalaryComponent.objects.filter(company=company).select_related(
            "employee", "gl_account")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Pay Elements"
        ctx["company_rows"] = [c for c in ctx["components"] if c.employee_id is None]
        ctx["people_rows"] = [c for c in ctx["components"] if c.employee_id]
        return ctx


class ComponentCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    model = SalaryComponent
    form_class = SalaryComponentForm
    template_name = "payroll/component_form.html"
    success_url = reverse_lazy("payroll:component_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        messages.success(self.request, f"'{form.instance.name}' saved.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        _form_class(ctx["form"])
        ctx["page_title"] = "New Pay Element"
        ctx["action"] = "Create"
        return ctx


class ComponentUpdateView(LoginRequiredMixin, UpdateView):
    model = SalaryComponent
    form_class = SalaryComponentForm
    template_name = "payroll/component_form.html"
    success_url = reverse_lazy("payroll:component_list")

    def get_queryset(self):
        return SalaryComponent.objects.filter(company=self.request.active_company)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def form_valid(self, form):
        messages.success(self.request, f"'{form.instance.name}' updated.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        _form_class(ctx["form"])
        ctx["page_title"] = f"Edit: {self.object.name}"
        ctx["action"] = "Save changes"
        return ctx
