"""Accounting views."""
import datetime
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum, Count
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, TemplateView, UpdateView

from apps.core.mixins import CompanyRequiredMixin

from . import gl_service
from .currency import book_currency
from .forms import (
    AccountForm, CurrencyForm, ExchangeRateForm, FiscalYearForm,
    JournalEntryForm, JournalEntryLineFormSet,
)
from .models import (
    Account, Currency, ExchangeRate, JournalEntry, JournalEntryLine, period_for,
)
from apps.core.forms import ISO_DATE


class AccountListView(LoginRequiredMixin, ListView):
    template_name = "accounting/account_list.html"
    context_object_name = "accounts"
    paginate_by = 100

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Account.objects.none()
        qs = Account.objects.filter(company=company).select_related("parent")
        state = self.request.GET.get("state", "active")
        if state == "inactive":
            qs = qs.filter(is_active=False)
        elif state != "all":
            state = "active"
            qs = qs.filter(is_active=True)
        account_type = self.request.GET.get("type")
        if account_type:
            qs = qs.filter(account_type=account_type)
        q = self.request.GET.get("q")
        if q:
            qs = qs.filter(Q(code__icontains=q) | Q(name__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        ctx["page_title"] = "Chart of Accounts"
        ctx["account_types"] = Account._meta.get_field("account_type").choices
        ctx["selected_type"] = self.request.GET.get("type", "")
        ctx["q"] = self.request.GET.get("q", "")
        ctx["state"] = self.request.GET.get("state", "active")
        if company:
            all_accounts = Account.objects.filter(company=company)
            ctx["active_count"] = all_accounts.filter(is_active=True).count()
            ctx["inactive_count"] = all_accounts.filter(is_active=False).count()
        return ctx


class AccountCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
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


class AccountUpdateView(LoginRequiredMixin, UpdateView):
    model = Account
    form_class = AccountForm
    template_name = "accounting/account_form.html"
    success_url = reverse_lazy("accounting:account_list")

    def get_queryset(self):
        return Account.objects.filter(company=self.request.active_company)

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


@login_required
@require_POST
def account_deactivate(request, pk):
    """Retire an account. Its history stays in the ledger; it simply stops
    being offered on journal lines, bills and opening balances."""
    account = get_object_or_404(Account, pk=pk, company=request.active_company)
    children = account.children.filter(is_active=True)
    if children.exists():
        messages.error(
            request,
            f"{account.code} still has {children.count()} active child account(s) — "
            "move or retire them first.",
        )
        return redirect("accounting:account_edit", pk=account.pk)
    mapped = account.bank_accounts.filter(is_active=True)
    if mapped.exists():
        messages.error(
            request,
            f"{account.code} is the ledger account behind "
            f"{', '.join(a.name for a in mapped)} — remap those before retiring it.",
        )
        return redirect("accounting:account_edit", pk=account.pk)
    balance = account.current_balance
    if balance:
        # A retired account drops off the entry forms and the ledger index, so
        # money left on it could no longer be moved off it or even be seen
        # from the screens a bookkeeper works in.
        messages.error(
            request,
            f"{account.code} still holds {balance:,.2f}. Move the balance to another "
            "account with a journal first; only an empty account can be retired.",
        )
        return redirect("accounting:account_edit", pk=account.pk)
    account.soft_delete()
    messages.success(
        request,
        f"{account.code} {account.name} retired. It no longer appears on entry "
        "forms; show it with the Retired filter to bring it back.",
    )
    return redirect("accounting:account_list")


@login_required
@require_POST
def account_activate(request, pk):
    account = get_object_or_404(Account, pk=pk, company=request.active_company)
    account.is_active = True
    account.save(update_fields=["is_active", "updated_at"])
    messages.success(request, f"{account.code} {account.name} is active again.")
    return redirect("accounting:account_list")


class JournalEntryListView(LoginRequiredMixin, ListView):
    template_name = "accounting/journal_entry_list.html"
    context_object_name = "entries"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return JournalEntry.objects.none()
        qs = JournalEntry.objects.filter(company=company).select_related(
            "posted_by", "reversed_by"
        )
        status = self.request.GET.get("status")
        if status == "REVERSED":
            # A reversed entry stays POSTED in the ledger — its reversal is a
            # second entry that offsets it — so this filter follows the link.
            qs = qs.filter(reversed_by__isnull=False)
        elif status:
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


class JournalEntryCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    model = JournalEntry
    form_class = JournalEntryForm
    template_name = "accounting/journal_entry_form.html"

    def get_line_formset(self):
        form_kwargs = {"company": self.request.active_company}
        if self.request.POST:
            return JournalEntryLineFormSet(
                self.request.POST, prefix="lines", form_kwargs=form_kwargs
            )
        return JournalEntryLineFormSet(prefix="lines", form_kwargs=form_kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["line_formset"] = kwargs.pop("line_formset", None) or self.get_line_formset()
        company = self.request.active_company
        if company:
            ctx["accounts"] = Account.objects.filter(company=company, is_active=True).values("id", "code", "name")
        ctx["page_title"] = "New Journal Entry"
        ctx["editing"] = False
        return ctx

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        import datetime
        form.fields["posting_date"].initial = datetime.date.today()
        return form

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        formset = self.get_line_formset()
        if not formset.is_valid():
            return self.render_to_response(self.get_context_data(form=form, line_formset=formset))
        with transaction.atomic():
            self.object = form.save()
            formset.instance = self.object
            formset.save()
        messages.success(self.request, f"Journal entry '{self.object.reference}' saved as DRAFT.")
        return redirect("accounting:journal_entry_detail", pk=self.object.pk)


class JournalEntryUpdateView(LoginRequiredMixin, CompanyRequiredMixin, UpdateView):
    """Drafts are the only entries anyone may edit. A posted entry is history;
    it gets reversed and replaced, never rewritten."""
    model = JournalEntry
    form_class = JournalEntryForm
    template_name = "accounting/journal_entry_form.html"

    def get_queryset(self):
        return JournalEntry.objects.filter(
            company=self.request.active_company, status="DRAFT"
        )

    def get_line_formset(self):
        form_kwargs = {"company": self.request.active_company}
        if self.request.POST:
            return JournalEntryLineFormSet(
                self.request.POST, instance=self.object, prefix="lines",
                form_kwargs=form_kwargs,
            )
        return JournalEntryLineFormSet(
            instance=self.object, prefix="lines", form_kwargs=form_kwargs
        )

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["line_formset"] = kwargs.pop("line_formset", None) or self.get_line_formset()
        ctx["accounts"] = Account.objects.filter(
            company=self.request.active_company, is_active=True
        ).values("id", "code", "name")
        ctx["page_title"] = f"Edit {self.object.reference}"
        ctx["editing"] = True
        return ctx

    def form_valid(self, form):
        formset = self.get_line_formset()
        if not formset.is_valid():
            return self.render_to_response(self.get_context_data(form=form, line_formset=formset))
        with transaction.atomic():
            self.object = form.save()
            formset.instance = self.object
            formset.save()
        messages.success(self.request, f"Draft {self.object.reference} updated.")
        return redirect("accounting:journal_entry_detail", pk=self.object.pk)


class JournalEntryDetailView(LoginRequiredMixin, DetailView):
    model = JournalEntry
    template_name = "accounting/journal_entry_detail.html"
    context_object_name = "entry"

    def get_queryset(self):
        return JournalEntry.objects.filter(company=self.request.active_company)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Journal Entry: {self.object.reference}"
        ctx["lines"] = self.object.lines.select_related("account")
        # The Reverse button is only offered where it would be honoured; any
        # other posted entry says where it is answered instead.
        refusal = (self.object.generic_reversal_refusal()
                   if self.object.status == "POSTED" and not self.object.reversed_by_id else "")
        ctx["can_reverse"] = self.object.status == "POSTED" and not refusal \
            and not self.object.reversed_by_id
        ctx["reverse_elsewhere"] = refusal
        return ctx


@login_required
@require_POST
def journal_entry_post(request, pk):
    entry = get_object_or_404(JournalEntry, pk=pk, company=request.active_company)
    try:
        entry.post(user=request.user)
        messages.success(request, f"Entry {entry.reference} posted successfully.")
        if entry.calendar_warning:
            messages.warning(request, entry.calendar_warning)
    except ValidationError as e:
        messages.error(request, " ".join(e.messages))
    except Exception as e:
        messages.error(request, str(e))
    return redirect("accounting:journal_entry_detail", pk=pk)


@login_required
@require_POST
def journal_entry_reverse(request, pk):
    """Offset a journal a person typed, with a second entry on the same date.

    Only hand-made journals are reversed here. An entry written by an invoice,
    a payroll run, a depreciation charge, a revaluation or a close belongs to
    that document, and its own screen reverses the entry and the document
    together; offsetting it from here would leave the two telling different
    stories. The refusal names the screen to use instead.
    """
    entry = get_object_or_404(JournalEntry, pk=pk, company=request.active_company)
    refusal = entry.generic_reversal_refusal()
    if refusal:
        messages.error(request, f"{entry.reference} was not reversed. {refusal}")
        return redirect("accounting:journal_entry_detail", pk=pk)
    try:
        reversal = entry.reverse(user=request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("accounting:journal_entry_detail", pk=pk)
    messages.success(request, f"Reversal entry {reversal.reference} created and posted.")
    return redirect("accounting:journal_entry_detail", pk=reversal.pk)


@login_required
@require_POST
def journal_entry_cancel(request, pk):
    """Throw away an unposted draft. Posted entries are history and can only
    ever be reversed, so this refuses them outright."""
    entry = get_object_or_404(JournalEntry, pk=pk, company=request.active_company)
    if entry.status != "DRAFT":
        messages.error(
            request,
            f"{entry.reference} is {entry.get_status_display().lower()}; "
            "only a draft can be cancelled.",
        )
        return redirect("accounting:journal_entry_detail", pk=pk)
    entry.status = "CANCELLED"
    entry.save(update_fields=["status", "updated_at"])
    messages.success(request, f"Draft {entry.reference} cancelled.")
    return redirect("accounting:journal_entry_list")


class TrialBalanceView(LoginRequiredMixin, TemplateView):
    """Every posted debit and credit, per account, up to and including a day.

    Read as at a date — today unless another is asked for — because a receipt
    dated next week has not happened yet; counting it would print a forecast
    under a heading that promises the books as they stand.
    """
    template_name = "accounting/trial_balance.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Trial Balance"
        company = self.request.active_company
        if not company:
            return ctx
        as_at = datetime.date.today()
        raw = self.request.GET.get("date") or ""
        if raw:
            try:
                as_at = datetime.date.fromisoformat(raw)
            except ValueError:
                messages.error(self.request, f"'{raw}' is not a date; showing today instead.")
        ctx["as_at"] = as_at
        # Every account with a balance belongs on the trial balance, retired or
        # not. Skipping a retired account that still holds money unbalances the
        # books and hides the mistake from the only report that would show it.
        accounts = Account.objects.filter(company=company).order_by("code")
        rows = []
        total_dr = Decimal("0")
        total_cr = Decimal("0")
        for acc in accounts:
            lines = acc.journal_lines.filter(
                journal_entry__status="POSTED", journal_entry__posting_date__lte=as_at)
            dr = lines.aggregate(t=Sum("debit_amount"))["t"] or Decimal("0")
            cr = lines.aggregate(t=Sum("credit_amount"))["t"] or Decimal("0")
            if dr > 0 or cr > 0:
                rows.append({"account": acc, "debit": dr, "credit": cr,
                             "retired": not acc.is_active})
                total_dr += dr
                total_cr += cr
        ctx["rows"] = rows
        ctx["total_debit"] = total_dr
        ctx["total_credit"] = total_cr
        ctx["is_balanced"] = abs(total_dr - total_cr) < Decimal("0.01")
        return ctx


class AccountLedgerView(LoginRequiredMixin, TemplateView):
    """General-ledger view for a single account: opening balance,
    every posted line, running balance, closing balance."""
    template_name = "accounting/account_ledger.html"

    def get_context_data(self, **kwargs):
        from datetime import datetime
        ctx = super().get_context_data(**kwargs)
        account = get_object_or_404(
            Account, pk=kwargs["pk"], company=self.request.active_company,
        )

        def as_date(text):
            """A hand-typed ?from= must not reach the ORM -- an unparseable
            date there raises and takes the page down with a 500."""
            try:
                return datetime.strptime(text, "%Y-%m-%d").date()
            except (TypeError, ValueError):
                return None

        start = as_date(self.request.GET.get("from"))
        end = as_date(self.request.GET.get("to"))

        posted = JournalEntryLine.objects.filter(
            account=account, journal_entry__status="POSTED",
        ).select_related("journal_entry")

        # Opening balance is whatever was posted strictly before the window.
        # Without it a filtered ledger silently starts from nothing and the
        # closing figure disagrees with the trial balance.
        sign = 1 if account.normal_balance == "DEBIT" else -1
        opening = Decimal("0")
        if start:
            before = posted.filter(journal_entry__posting_date__lt=start).aggregate(
                dr=Sum("debit_amount"), cr=Sum("credit_amount"),
            )
            opening = Decimal(str((before["dr"] or 0) - (before["cr"] or 0))) * sign

        lines = posted
        if start:
            lines = lines.filter(journal_entry__posting_date__gte=start)
        if end:
            lines = lines.filter(journal_entry__posting_date__lte=end)
        lines = lines.order_by(
            "journal_entry__posting_date", "journal_entry__created_at", "id",
        )

        running = opening
        rows = []
        for ln in lines:
            running += (ln.debit_amount - ln.credit_amount) * sign
            rows.append({
                "date": ln.journal_entry.posting_date,
                "reference": ln.journal_entry.reference,
                "description": ln.description or ln.journal_entry.notes,
                "debit": ln.debit_amount,
                "credit": ln.credit_amount,
                "balance": running,
                "entry_pk": ln.journal_entry_id,
            })

        ctx["account"] = account
        ctx["rows"] = rows
        ctx["opening_balance"] = opening
        ctx["closing_balance"] = running
        ctx["total_debit"] = sum(r["debit"] for r in rows)
        ctx["total_credit"] = sum(r["credit"] for r in rows)
        ctx["from_date"] = start
        ctx["to_date"] = end
        ctx["bank_accounts"] = account.bank_accounts.filter(is_active=True)
        ctx["reconcilable"] = account.is_reconcilable
        ctx["line_count"] = len(rows)
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
from apps.organizations.models import FiscalYear, AccountingPeriod


class FiscalYearListView(LoginRequiredMixin, ListView):
    template_name = "accounting/fiscal_year_list.html"
    context_object_name = "fiscal_years"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return FiscalYear.objects.none()
        return FiscalYear.objects.filter(company=company).prefetch_related("periods")


class FiscalYearCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    model = FiscalYear
    form_class = FiscalYearForm
    template_name = "accounting/fiscal_year_form.html"
    success_url = reverse_lazy("accounting:fiscal_year_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

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
    fy = get_object_or_404(FiscalYear, pk=pk, company=request.active_company)
    if fy.is_closed:
        messages.info(request, f"Fiscal year {fy.name} is already closed.")
        return redirect("accounting:fiscal_year_list")
    try:
        entries = gl_service.close_fiscal_year(request.active_company, fy, user=request.user)
    except ValidationError as e:
        messages.error(request, f"Year end failed: {' '.join(e.messages)}")
        return redirect("accounting:fiscal_year_list")
    except Exception as e:
        messages.error(request, f"Year end failed: {e}")
        return redirect("accounting:fiscal_year_list")
    carried = [je for je in entries if je.source_document_type == gl_service.YEAR_CLOSE]
    messages.success(
        request,
        f"Fiscal year {fy.name} closed with {len(entries)} journal entry(s). "
        + (f"{carried[0].reference} moved the year's result to retained earnings."
           if carried else "Current year profit was already zero, so nothing moved to retained earnings."),
    )
    return redirect("accounting:fiscal_year_list")


@login_required
@require_POST
def fiscal_year_reopen(request, pk):
    fy = get_object_or_404(FiscalYear, pk=pk, company=request.active_company)
    try:
        reversed_entries = gl_service.reopen_fiscal_year(
            request.active_company, fy, user=request.user,
        )
    except ValidationError as e:
        messages.error(request, f"{fy.name} was not reopened. {' '.join(e.messages)}")
        return redirect("accounting:fiscal_year_list")
    messages.success(
        request,
        f"Fiscal year {fy.name} reopened."
        + (f" {len(reversed_entries)} closing entry(s) reversed." if reversed_entries else ""),
    )
    return redirect("accounting:fiscal_year_list")


@login_required
@require_POST
def period_close(request, pk):
    period = get_object_or_404(AccountingPeriod, pk=pk, fiscal_year__company=request.active_company)
    if period.status != "OPEN":
        messages.info(request, f"Period {period.name} is already {period.get_status_display().lower()}.")
        return redirect("accounting:fiscal_year_list")
    try:
        entry = gl_service.close_period(request.active_company, period, user=request.user)
    except ValidationError as e:
        messages.error(request, f"Closing {period.name} failed: {' '.join(e.messages)}")
        return redirect("accounting:fiscal_year_list")
    except Exception as e:
        messages.error(request, f"Closing {period.name} failed: {e}")
        return redirect("accounting:fiscal_year_list")
    profit = gl_service.profit_for(request.active_company, period.start_date, period.end_date)
    messages.success(
        request,
        f"Period {period.name} closed."
        + (f" {entry.reference} carried {profit:,.2f} to current year profit." if entry
           else " The period held no income or expense, so there was nothing to carry."),
    )
    return redirect("accounting:fiscal_year_list")


@login_required
@require_POST
def period_reopen(request, pk):
    period = get_object_or_404(AccountingPeriod, pk=pk, fiscal_year__company=request.active_company)
    try:
        entry = gl_service.reopen_period(request.active_company, period, user=request.user)
    except ValidationError as e:
        messages.error(request, f"{period.name} was not reopened. {' '.join(e.messages)}")
        return redirect("accounting:fiscal_year_list")
    messages.success(
        request,
        f"Period {period.name} reopened."
        + (f" Closing entry {entry.reference} was reversed." if entry else ""),
    )
    return redirect("accounting:fiscal_year_list")


def _opening_amount(raw):
    """Read one opening-balance box. Returns (amount, problem).

    The boxes are plain text in the browser, so anything can arrive: NaN and
    Infinity parse as Decimals, 1e20 does not fit the ledger's columns, and
    half a cent typed twice passes a sum check and then fails at the cent. A
    figure is accepted only as a finite, non-negative amount of at most two
    decimals that the ledger can store.
    """
    from decimal import InvalidOperation

    text = (raw or "").strip().replace(",", "")
    if not text:
        return Decimal("0"), ""
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        return Decimal("0"), f"'{raw}' is not a number."
    if not amount.is_finite():
        return Decimal("0"), f"'{raw}' is not an amount."
    if amount < 0:
        return Decimal("0"), ("amounts cannot be negative; put the figure in the "
                              "other column instead.")
    if amount >= Decimal("1e13"):
        return Decimal("0"), f"{raw} is larger than the ledger can hold."
    if amount != amount.quantize(Decimal("0.01")):
        return Decimal("0"), f"{raw} has more than two decimals; enter whole cents."
    return amount.quantize(Decimal("0.01")), ""


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

        # One live opening entry per book. A second one would add the same
        # share capital and the same bank balance on top of the first; a wrong
        # opening is put right by reversing it and entering it again.
        live = JournalEntry.objects.filter(
            company=company, entry_type="OPENING", status="POSTED",
            reversed_by__isnull=True, reversal_of__isnull=True,
        ).order_by("posting_date").first()
        if live:
            messages.error(
                request,
                f"This book already has its opening balances in {live.reference} "
                f"({live.posting_date:%d %b %Y}). To change them, reverse "
                f"{live.reference} from its journal page and enter them again here.")
            return redirect("accounting:opening_balances")

        raw_date = (request.POST.get("posting_date") or "").strip()
        if not raw_date:
            messages.error(request, "Posting date is required.")
            return redirect("accounting:opening_balances")
        try:
            posting_date = datetime.date.fromisoformat(raw_date)
        except ValueError:
            messages.error(
                request, f"'{raw_date}' is not a real date. Pick the day the opening "
                "balances are taken at, usually the first day of the fiscal year.")
            return redirect("accounting:opening_balances")

        line_data, problems = [], []
        total_dr = Decimal("0")
        total_cr = Decimal("0")
        for acc in accounts:
            amounts = []
            for side in ("dr", "cr"):
                amount, problem = _opening_amount(request.POST.get(f"{side}_{acc.pk}"))
                if problem:
                    problems.append(f"{acc.code} {'debit' if side == 'dr' else 'credit'}: {problem}")
                amounts.append(amount)
            dr_dec, cr_dec = amounts
            if dr_dec > 0 and cr_dec > 0:
                problems.append(
                    f"{acc.code} has both a debit and a credit. An opening balance sits "
                    "on one side: enter only the net figure.")
                continue
            if dr_dec > 0 or cr_dec > 0:
                line_data.append((acc, dr_dec, cr_dec))
                total_dr += dr_dec
                total_cr += cr_dec

        if problems:
            messages.error(request, "Nothing was posted. " + " ".join(problems[:6])
                           + (" …" if len(problems) > 6 else ""))
            return redirect("accounting:opening_balances")
        if not line_data:
            messages.error(request, "No balances were entered.")
            return redirect("accounting:opening_balances")
        # Every figure is already in whole cents, so the two sides either agree
        # to the cent or they do not; there is no tolerance to hide behind.
        if total_dr != total_cr:
            messages.error(request, f"Opening balances must balance: Dr {total_dr} ≠ Cr {total_cr}.")
            return redirect("accounting:opening_balances")

        try:
            with transaction.atomic():
                je = JournalEntry.objects.create(
                    company=company,
                    reference=SequenceCounter.next_number(company, "OB-"),
                    entry_type="OPENING", posting_date=posting_date,
                    notes="Opening balances",
                )
                for acc, dr_dec, cr_dec in line_data:
                    JournalEntryLine.objects.create(
                        journal_entry=je, account=acc,
                        description=f"Opening balance — {acc.name}",
                        debit_amount=dr_dec, credit_amount=cr_dec,
                    )
                # Post it through the model, so an opening entry is checked and
                # dated like every other entry instead of being stamped POSTED.
                je.post(user=request.user)
        except ValidationError as exc:
            messages.error(request, "Nothing was posted. " + " ".join(exc.messages))
            return redirect("accounting:opening_balances")
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


# ─────────────────────────── currencies and rates ────────────────────────────

class CurrencyListView(LoginRequiredMixin, TemplateView):
    """Every currency the book knows, with the latest rate filed for it.

    A currency with no rate is listed but marked unusable: the register of
    currencies is shared, while what makes one transactable is this company
    having said what it was worth.
    """
    template_name = "accounting/currency_list.html"

    def get_context_data(self, **kwargs):
        import datetime
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        ctx["page_title"] = "Currencies & Exchange Rates"
        if not company:
            return ctx
        book = (company.currency or "KES").upper()
        filed = ExchangeRate.objects.filter(company=company).select_related("currency")
        latest = {}
        for rate in filed.order_by("effective_date"):
            latest[rate.currency.code] = rate
        counts = filed.values("currency__code").annotate(n=Count("pk"))
        priced = {row["currency__code"]: row["n"] for row in counts}

        today = datetime.date.today()
        rows, transactable = [], 0
        for currency in Currency.objects.filter(is_active=True).order_by("code"):
            rate = latest.get(currency.code)
            if currency.code == book:
                note, usable = "This book is kept in it — always worth 1.", True
            elif rate:
                days = (today - rate.effective_date).days
                note = f"Filed {rate.effective_date:%d %b %Y}" + (
                    f" · {days} days old" if days > 7 else "")
                usable = True
                transactable += 1
            else:
                note, usable = "No rate filed — documents cannot be raised in it.", False
            rows.append({
                "currency": currency, "rate": rate, "count": priced.get(currency.code, 0),
                "note": note, "usable": usable, "is_book": currency.code == book,
            })
        ctx["rows"] = rows
        ctx["book"] = book
        ctx["transactable"] = transactable
        ctx["recent"] = filed.order_by("-effective_date", "-created_at")[:8]
        return ctx


class CurrencyCreateView(LoginRequiredMixin, CreateView):
    model = Currency
    form_class = CurrencyForm
    template_name = "accounting/currency_form.html"
    success_url = reverse_lazy("accounting:currency_list")

    def form_valid(self, form):
        messages.success(
            self.request,
            f"{form.instance.code} is registered. File a rate for it before "
            f"raising a document in it.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Register a Currency"
        return ctx


class _ExchangeRateCompanyMixin:
    """Rates belong to the company that filed them, never to the currency."""

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs


class ExchangeRateCreateView(_ExchangeRateCompanyMixin, LoginRequiredMixin,
                             CompanyRequiredMixin, CreateView):
    model = ExchangeRate
    form_class = ExchangeRateForm
    template_name = "accounting/exchange_rate_form.html"
    success_url = reverse_lazy("accounting:currency_list")

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        response = super().form_valid(form)
        messages.success(
            self.request,
            f"1 {self.object.currency.code} = {self.object.rate} "
            f"{self.object.company.currency} on {self.object.effective_date:%d %b %Y}.")
        return response

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "File an Exchange Rate"
        return ctx


class ExchangeRateUpdateView(_ExchangeRateCompanyMixin, LoginRequiredMixin,
                             UpdateView):
    model = ExchangeRate
    form_class = ExchangeRateForm
    template_name = "accounting/exchange_rate_form.html"
    success_url = reverse_lazy("accounting:currency_list")

    def get_queryset(self):
        return ExchangeRate.objects.filter(company=self.request.active_company)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit the {self.object.currency_id} rate"
        return ctx


class ExchangeRateHistoryView(LoginRequiredMixin, ListView):
    """The whole rate history of one currency — the paper trail behind a claim."""
    template_name = "accounting/exchange_rate_list.html"
    context_object_name = "rates"
    paginate_by = 50

    def get_queryset(self):
        return ExchangeRate.objects.filter(
            company=self.request.active_company,
            currency__code=self.kwargs["code"]).order_by("-effective_date")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["code"] = self.kwargs["code"]
        ctx["page_title"] = f"Rate history — {self.kwargs['code']}"
        return ctx


class RevaluationView(LoginRequiredMixin, TemplateView):
    """What the open foreign balances would be worth on the day you choose.

    A preview is not a posting: nothing here touches the ledger until the
    accountant says so, because the right date to value at is theirs to pick —
    a period end, a year end, or the day the bank asked for a statement.
    """
    template_name = "accounting/revaluation.html"

    def get_context_data(self, **kwargs):
        from apps.accounting import revaluation
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Exchange Revaluation"
        company = self.request.active_company
        if not company:
            return ctx

        raw = self.request.GET.get("date") or ""
        try:
            as_of = datetime.date.fromisoformat(raw)
        except ValueError:
            as_of = datetime.date.today()
        ctx["as_of"] = as_of

        position = revaluation.positions(company, as_of)
        receivable, payable = revaluation.summary(position)
        ctx["position"] = position
        ctx["book"] = position["book"]
        ctx["total_receivable"] = receivable
        ctx["total_payable"] = payable
        ctx["total_difference"] = receivable - payable
        ctx["live"] = revaluation.live_revaluation(company)
        ctx["valuation"] = [
            account for account in (
                Account.objects.filter(company=company, code=code).first()
                for code in (revaluation.RECEIVABLE_ACCOUNT,
                             revaluation.PAYABLE_ACCOUNT))
            if account
        ]
        period = period_for(company, as_of)
        ctx["period"] = period
        ctx["period_warn"] = bool(
            period and (period.status != "OPEN" or period.fiscal_year.is_closed))
        return ctx


@login_required
@require_POST
def revaluation_post(request):
    """Book the difference between the rates the documents were raised at and
    the rate of the day being reported."""
    from apps.accounting import revaluation

    company = request.active_company
    raw = request.POST.get("date") or ""
    try:
        as_of = datetime.date.fromisoformat(raw)
    except ValueError:
        messages.error(request, "Choose the day the balances are being read at.")
        return redirect("accounting:revaluation")

    try:
        entry, position = revaluation.post_revaluation(company, as_of, user=request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("accounting:revaluation")

    if entry is None:
        if position["rows"]:
            messages.info(
                request,
                f"{as_of:%d %b %Y}: the open foreign balances already sit at the "
                "day's rate, so there is nothing to move.")
        else:
            messages.info(
                request,
                f"{as_of:%d %b %Y}: no open document in a foreign currency, "
                "so there is nothing to revalue.")
        return redirect("accounting:revaluation")

    messages.success(
        request,
        f"Revaluation {entry.reference} posted for {as_of:%d %b %Y}: "
        f"{entry.total_debit:,.2f} {book_currency(company)} of unrealised exchange "
        "difference booked. The control accounts were left alone.")
    return redirect("accounting:journal_entry_detail", pk=entry.pk)


@login_required
@require_POST
def revaluation_reverse(request):
    """Take the current revaluation off the balance sheet."""
    from apps.accounting import revaluation

    company = request.active_company
    entry = revaluation.live_revaluation(company)
    if not entry:
        messages.info(request, "The book carries no revaluation to reverse.")
        return redirect("accounting:revaluation")
    raw = request.POST.get("date") or ""
    try:
        as_of = datetime.date.fromisoformat(raw)
    except ValueError:
        as_of = None
    if as_of and as_of < entry.posting_date:
        messages.error(
            request,
            f"{entry.reference} is dated {entry.posting_date:%d %b %Y}; a reversal "
            "cannot be dated before the entry it answers.")
        return redirect("accounting:revaluation")
    try:
        # The releases made against it as documents were settled come off with
        # it; left behind, they would be undoing a revaluation no longer there.
        reversal = revaluation.take_off(entry, user=request.user, posting_date=as_of)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("accounting:revaluation")
    messages.success(
        request,
        f"Revaluation {entry.reference} reversed by {reversal.reference}. "
        "The foreign balances are carried at their booked rates again.")
    return redirect("accounting:journal_entry_detail", pk=reversal.pk)
