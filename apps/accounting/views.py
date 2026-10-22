"""Accounting views."""
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, TemplateView, UpdateView

from apps.core.scoping import CompanyScopedMixin, get_scoped_or_404, scope

from .forms import AccountForm, JournalEntryForm, JournalEntryLineFormSet
from .models import Account, JournalEntry, JournalEntryLine, TaxRate


class AccountListView(LoginRequiredMixin, ListView):
    template_name = "accounting/account_list.html"
    context_object_name = "accounts"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Account.objects.none()
        qs = Account.objects.filter(company=company, is_active=True).select_related("parent")
        account_type = self.request.GET.get("type")
        if account_type:
            qs = qs.filter(account_type=account_type)
        q = self.request.GET.get("q")
        if q:
            qs = qs.filter(Q(code__icontains=q) | Q(name__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Chart of Accounts"
        ctx["account_types"] = Account._meta.get_field("account_type").choices
        ctx["selected_type"] = self.request.GET.get("type", "")
        ctx["q"] = self.request.GET.get("q", "")
        company = self.request.active_company
        ctx["chart_is_empty"] = bool(company) and not Account.objects.filter(company=company).exists()
        return ctx


class AccountCreateView(LoginRequiredMixin, CreateView):
    model = Account
    form_class = AccountForm
    template_name = "accounting/account_form.html"
    success_url = reverse_lazy("accounting:account_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        messages.success(self.request, f"Account '{form.instance.name}' created.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "New Account"
        ctx["action"] = "Create"
        return ctx


class AccountUpdateView(CompanyScopedMixin, LoginRequiredMixin, UpdateView):
    model = Account
    form_class = AccountForm
    template_name = "accounting/account_form.html"
    success_url = reverse_lazy("accounting:account_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def form_valid(self, form):
        messages.success(self.request, f"Account '{form.instance.name}' updated.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit: {self.object.name}"
        ctx["action"] = "Save Changes"
        return ctx


class JournalEntryListView(LoginRequiredMixin, ListView):
    template_name = "accounting/journal_entry_list.html"
    context_object_name = "entries"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return JournalEntry.objects.none()
        qs = JournalEntry.objects.filter(company=company).select_related("posted_by")
        status = self.request.GET.get("status")
        if status:
            qs = qs.filter(status=status)
        q = self.request.GET.get("q")
        if q:
            qs = qs.filter(Q(reference__icontains=q) | Q(notes__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Journal Entries"
        ctx["status_filter"] = self.request.GET.get("status", "")
        ctx["q"] = self.request.GET.get("q", "")
        ctx["status_choices"] = JournalEntry._meta.get_field("status").choices
        return ctx


class JournalEntryCreateView(LoginRequiredMixin, CreateView):
    model = JournalEntry
    form_class = JournalEntryForm
    template_name = "accounting/journal_entry_form.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if "line_formset" not in ctx:
            formset_kwargs = {"prefix": "lines", "form_kwargs": {"company": company}}
            if self.request.method == "POST":
                ctx["line_formset"] = JournalEntryLineFormSet(self.request.POST, **formset_kwargs)
            else:
                ctx["line_formset"] = JournalEntryLineFormSet(**formset_kwargs)
        ctx["accounts"] = scope(Account.objects.filter(is_active=True), company).values("id", "code", "name")
        ctx["page_title"] = "New Journal Entry"
        return ctx

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        company = self.request.active_company
        if company:
            from apps.core.models import SequenceCounter
            next_ref = SequenceCounter.next_number(company, "JE-")
            form.fields["reference"].initial = next_ref
        import datetime
        form.fields["posting_date"].initial = datetime.date.today()
        return form

    def form_valid(self, form):
        from django.db import transaction
        company = self.request.active_company
        if company is None:
            messages.error(self.request, "Select a company before creating journal entries.")
            return self.form_invalid(form)
        form.instance.company = company
        ctx = self.get_context_data(form=form)
        formset = ctx["line_formset"]
        if formset.is_valid():
            with transaction.atomic():
                self.object = form.save()
                formset.instance = self.object
                formset.save()
            messages.success(self.request, f"Journal entry '{self.object.reference}' created.")
            return redirect("accounting:journal_entry_detail", pk=self.object.pk)
        return self.render_to_response(ctx)


class JournalEntryDetailView(CompanyScopedMixin, LoginRequiredMixin, DetailView):
    model = JournalEntry
    template_name = "accounting/journal_entry_detail.html"
    context_object_name = "entry"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Journal Entry: {self.object.reference}"
        ctx["lines"] = self.object.lines.select_related("account")
        return ctx


@login_required
def journal_entry_post(request, pk):
    entry = get_scoped_or_404(JournalEntry, request, pk=pk)
    if request.method == "POST":
        try:
            entry.post(user=request.user)
            messages.success(request, f"Entry {entry.reference} posted successfully.")
        except Exception as e:
            messages.error(request, " ".join(getattr(e, "messages", [str(e)])))
    return redirect("accounting:journal_entry_detail", pk=pk)


@login_required
def journal_entry_reverse(request, pk):
    entry = get_scoped_or_404(JournalEntry, request, pk=pk)
    if request.method == "POST":
        try:
            reversal = entry.reverse(user=request.user)
            messages.success(request, f"Reversal entry {reversal.reference} created and posted.")
            return redirect("accounting:journal_entry_detail", pk=reversal.pk)
        except Exception as e:
            messages.error(request, " ".join(getattr(e, "messages", [str(e)])))
    return redirect("accounting:journal_entry_detail", pk=pk)


class TrialBalanceView(LoginRequiredMixin, TemplateView):
    template_name = "accounting/trial_balance.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Trial Balance"
        company = self.request.active_company
        if not company:
            return ctx
        from .reports import trial_balance
        ctx.update(trial_balance(company))
        return ctx


class AccountLedgerView(LoginRequiredMixin, TemplateView):
    """General-ledger view for a single account: opening balance,
    every posted line, running balance, closing balance."""
    template_name = "accounting/account_ledger.html"

    def get_context_data(self, **kwargs):
        from datetime import date
        ctx = super().get_context_data(**kwargs)
        account = get_scoped_or_404(Account, self.request, pk=kwargs["pk"])
        start = self.request.GET.get("from") or ""
        end = self.request.GET.get("to") or ""

        lines = JournalEntryLine.objects.filter(
            account=account, journal_entry__status="POSTED",
        ).select_related("journal_entry").order_by(
            "journal_entry__posting_date", "journal_entry__created_at", "id",
        )
        if start:
            lines = lines.filter(journal_entry__posting_date__gte=start)
        if end:
            lines = lines.filter(journal_entry__posting_date__lte=end)

        running = Decimal("0")
        rows = []
        for ln in lines:
            running += (ln.debit_amount - ln.credit_amount)
            rows.append({
                "date": ln.journal_entry.posting_date,
                "reference": ln.journal_entry.reference,
                "description": ln.description or ln.journal_entry.notes,
                "debit": ln.debit_amount,
                "credit": ln.credit_amount,
                "balance": running,
            })

        ctx["account"] = account
        ctx["rows"] = rows
        ctx["opening_balance"] = Decimal("0")
        ctx["closing_balance"] = running
        ctx["total_debit"] = sum(r["debit"] for r in rows)
        ctx["total_credit"] = sum(r["credit"] for r in rows)
        ctx["from_date"] = start
        ctx["to_date"] = end
        return ctx


class GeneralLedgerIndexView(LoginRequiredMixin, TemplateView):
    """Chart-of-accounts tree grouped by account type with current balances."""
    template_name = "accounting/general_ledger.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if not company:
            return ctx
        accounts = Account.objects.filter(company=company, is_active=True).order_by("code")
        buckets = {}
        for a in accounts:
            buckets.setdefault(a.get_account_type_display(), []).append(a)
        ctx["buckets"] = buckets
        return ctx


# ------------------------- Accounting Periods -------------------------
from django.views.decorators.http import require_POST
from apps.organizations.models import FiscalYear, AccountingPeriod


class FiscalYearListView(LoginRequiredMixin, ListView):
    template_name = "accounting/fiscal_year_list.html"
    context_object_name = "fiscal_years"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return FiscalYear.objects.none()
        return FiscalYear.objects.filter(company=company).prefetch_related("periods")


class FiscalYearCreateView(LoginRequiredMixin, CreateView):
    model = FiscalYear
    template_name = "accounting/fiscal_year_form.html"
    fields = ["name", "start_date", "end_date", "is_current"]
    success_url = reverse_lazy("accounting:fiscal_year_list")

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        from django import forms as dforms
        for fname, field in form.fields.items():
            if isinstance(field.widget, dforms.DateInput):
                field.widget.attrs["type"] = "date"
                field.widget.attrs["class"] = "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
            elif isinstance(field.widget, dforms.CheckboxInput):
                field.widget.attrs["class"] = "form-checkbox"
            else:
                field.widget.attrs["class"] = "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
        return form

    def form_valid(self, form):
        import calendar
        from datetime import date, timedelta
        form.instance.company = self.request.active_company
        response = super().form_valid(form)
        fy = self.object
        # Auto-generate 12 monthly periods
        current = fy.start_date
        n = 1
        while current <= fy.end_date:
            last_day = calendar.monthrange(current.year, current.month)[1]
            period_end = date(current.year, current.month, last_day)
            if period_end > fy.end_date:
                period_end = fy.end_date
            AccountingPeriod.objects.get_or_create(
                fiscal_year=fy, name=f"P{n:02d} {current.strftime('%b %Y')}",
                defaults={"start_date": current, "end_date": period_end, "status": "OPEN"},
            )
            # advance to first day of next month
            if current.month == 12:
                current = date(current.year + 1, 1, 1)
            else:
                current = date(current.year, current.month + 1, 1)
            n += 1
        messages.success(self.request, f"Fiscal year '{fy.name}' created with {n-1} monthly periods.")
        return response

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "New Fiscal Year"
        return ctx


@login_required
@require_POST
def fiscal_year_close(request, pk):
    fy = get_scoped_or_404(FiscalYear, request, pk=pk)
    fy.is_closed = True
    fy.save(update_fields=["is_closed"])
    fy.periods.update(status="LOCKED")
    messages.success(request, f"Fiscal year {fy.name} closed and all periods locked.")
    return redirect("accounting:fiscal_year_list")


@login_required
@require_POST
def fiscal_year_reopen(request, pk):
    fy = get_scoped_or_404(FiscalYear, request, pk=pk)
    fy.is_closed = False
    fy.save(update_fields=["is_closed"])
    fy.periods.filter(status="LOCKED").update(status="OPEN")
    messages.success(request, f"Fiscal year {fy.name} reopened.")
    return redirect("accounting:fiscal_year_list")


@login_required
@require_POST
def period_close(request, pk):
    period = get_scoped_or_404(AccountingPeriod, request, lookup="fiscal_year__company", pk=pk)
    period.status = "CLOSED"
    period.save(update_fields=["status"])
    messages.success(request, f"Period {period.name} closed.")
    return redirect("accounting:fiscal_year_list")


@login_required
@require_POST
def period_reopen(request, pk):
    period = get_scoped_or_404(AccountingPeriod, request, lookup="fiscal_year__company", pk=pk)
    period.status = "OPEN"
    period.save(update_fields=["status"])
    messages.success(request, f"Period {period.name} reopened.")
    return redirect("accounting:fiscal_year_list")


class OpeningBalanceView(LoginRequiredMixin, TemplateView):
    """Enter opening balances for a company's chart of accounts.
    POSTing creates a single balanced journal entry dated the fiscal year start.
    """
    template_name = "accounting/opening_balances.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if not company:
            return ctx
        ctx["accounts"] = Account.objects.filter(company=company, is_active=True).order_by("code")
        ctx["fiscal_years"] = FiscalYear.objects.filter(company=company)
        ctx["page_title"] = "Opening Balances"
        return ctx

    def post(self, request, *args, **kwargs):
        from django.utils import timezone
        from django.db import transaction
        from apps.core.models import SequenceCounter
        company = request.active_company
        accounts = Account.objects.filter(company=company, is_active=True)
        posting_date = request.POST.get("posting_date")
        if not posting_date:
            messages.error(request, "Posting date is required.")
            return redirect("accounting:opening_balances")

        line_data = []
        total_dr = Decimal("0")
        total_cr = Decimal("0")
        for acc in accounts:
            dr = request.POST.get(f"dr_{acc.pk}") or "0"
            cr = request.POST.get(f"cr_{acc.pk}") or "0"
            try:
                dr_dec = Decimal(dr or "0")
                cr_dec = Decimal(cr or "0")
            except Exception:
                continue
            if dr_dec < 0 or cr_dec < 0:
                messages.error(request, f"{acc.code} {acc.name}: amounts can't be negative — use the other column.")
                return redirect("accounting:opening_balances")
            if dr_dec > 0 and cr_dec > 0:
                messages.error(request, f"{acc.code} {acc.name}: enter a debit or a credit, not both.")
                return redirect("accounting:opening_balances")
            if dr_dec > 0 or cr_dec > 0:
                line_data.append((acc, dr_dec, cr_dec))
                total_dr += dr_dec
                total_cr += cr_dec

        if abs(total_dr - total_cr) > Decimal("0.01"):
            messages.error(request, f"Opening balances must balance: Dr {total_dr} ≠ Cr {total_cr}.")
            return redirect("accounting:opening_balances")
        if not line_data:
            messages.error(request, "No balances were entered.")
            return redirect("accounting:opening_balances")

        from django.core.exceptions import ValidationError
        from .models import assert_period_open
        try:
            assert_period_open(company, posting_date)
        except ValidationError as e:
            messages.error(request, " ".join(e.messages))
            return redirect("accounting:opening_balances")
        except ValueError:
            messages.error(request, "Enter the posting date as YYYY-MM-DD.")
            return redirect("accounting:opening_balances")

        with transaction.atomic():
            ref = f"OB-{SequenceCounter.next_number(company, 'OB-')}"
            je = JournalEntry.objects.create(
                company=company, reference=ref,
                entry_type="OPENING", posting_date=posting_date,
                notes="Opening balances", status="POSTED",
                posted_at=timezone.now(),
            )
            for acc, dr_dec, cr_dec in line_data:
                JournalEntryLine.objects.create(
                    journal_entry=je, account=acc,
                    description=f"Opening balance — {acc.name}",
                    debit_amount=dr_dec, credit_amount=cr_dec,
                )
        messages.success(request, f"Opening balance entry {je.reference} posted (Dr/Cr {total_dr}).")
        return redirect("accounting:journal_entry_detail", pk=je.pk)


class AccountingIndexView(LoginRequiredMixin, TemplateView):
    """Accounting / GL module dashboard."""
    template_name = "accounting/index.html"

    def get_context_data(self, **kwargs):
        from decimal import Decimal
        from datetime import date
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if not company:
            return ctx
        accs = Account.objects.filter(company=company, is_active=True)
        ctx["account_count"] = accs.count()
        ctx["asset_balance"] = sum((a.current_balance for a in accs.filter(account_type__in=["ASSET", "CASH", "BANK"])), Decimal(0))
        ctx["liability_balance"] = sum((a.current_balance for a in accs.filter(account_type="LIABILITY")), Decimal(0))
        ctx["equity_balance"] = sum((a.current_balance for a in accs.filter(account_type="EQUITY")), Decimal(0))
        ctx["recent_entries"] = JournalEntry.objects.filter(company=company).order_by("-posting_date", "-created_at")[:8]
        ctx["draft_count"] = JournalEntry.objects.filter(company=company, status="DRAFT").count()
        return ctx


# ---------------------------------------------------------------------------
# Standard chart + tax rates
# ---------------------------------------------------------------------------

@login_required
@require_POST
def account_load_standard(request):
    from .standard_chart import load_standard_chart
    company = request.active_company
    if company is None:
        messages.error(request, "Select a company first.")
        return redirect("accounting:account_list")
    created = load_standard_chart(company)
    if created:
        messages.success(request, f"Loaded the standard Kenyan chart: {created} accounts and the 16% VAT rate.")
    else:
        messages.info(request, "Your chart already has every standard account.")
    return redirect("accounting:account_list")


class TaxRateListView(CompanyScopedMixin, LoginRequiredMixin, ListView):
    model = TaxRate
    template_name = "accounting/tax_rate_list.html"
    context_object_name = "tax_rates"

    def get_queryset(self):
        return super().get_queryset().select_related("account").order_by("code")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Tax rates"
        return ctx


class _TaxRateFormMixin:
    template_name = "accounting/tax_rate_form.html"
    success_url = reverse_lazy("accounting:tax_rate_list")

    def get_form_class(self):
        from .forms import TaxRateForm
        return TaxRateForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        messages.success(self.request, f"Tax rate {form.instance.name} saved.")
        return super().form_valid(form)


class TaxRateCreateView(_TaxRateFormMixin, LoginRequiredMixin, CreateView):
    model = TaxRate

    def get_initial(self):
        import datetime
        return {"effective_from": datetime.date.today()}


class TaxRateUpdateView(_TaxRateFormMixin, CompanyScopedMixin, LoginRequiredMixin, UpdateView):
    model = TaxRate
