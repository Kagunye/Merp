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

from .forms import AccountForm, JournalEntryForm, JournalEntryLineFormSet
from .models import Account, JournalEntry, JournalEntryLine


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
        return ctx


class AccountCreateView(LoginRequiredMixin, CreateView):
    model = Account
    form_class = AccountForm
    template_name = "accounting/account_form.html"
    success_url = reverse_lazy("accounting:account_list")

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        company = self.request.active_company
        if company:
            form.fields["parent"].queryset = Account.objects.filter(company=company, is_active=True)
            form.fields["parent"].empty_label = "— No parent (top-level)"
        return form

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

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        company = self.request.active_company
        if company:
            form.fields["parent"].queryset = Account.objects.filter(
                company=company, is_active=True
            ).exclude(pk=self.object.pk)
        return form

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
        if self.request.POST:
            ctx["line_formset"] = JournalEntryLineFormSet(self.request.POST, prefix="lines")
        else:
            ctx["line_formset"] = JournalEntryLineFormSet(prefix="lines")
        company = self.request.active_company
        if company:
            ctx["accounts"] = Account.objects.filter(company=company, is_active=True).values("id", "code", "name")
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
        form.instance.company = self.request.active_company
        ctx = self.get_context_data(form=form)
        formset = ctx["line_formset"]
        if formset.is_valid():
            self.object = form.save()
            formset.instance = self.object
            formset.save()
            messages.success(self.request, f"Journal entry '{self.object.reference}' created.")
            return redirect("accounting:journal_entry_detail", pk=self.object.pk)
        return self.render_to_response(ctx)


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
        return ctx


@login_required
def journal_entry_post(request, pk):
    entry = get_object_or_404(JournalEntry, pk=pk, company=request.active_company)
    if request.method == "POST":
        try:
            entry.post(user=request.user)
            messages.success(request, f"Entry {entry.reference} posted successfully.")
        except Exception as e:
            messages.error(request, str(e))
    return redirect("accounting:journal_entry_detail", pk=pk)


@login_required
def journal_entry_reverse(request, pk):
    entry = get_object_or_404(JournalEntry, pk=pk, company=request.active_company)
    if request.method == "POST":
        try:
            reversal = entry.reverse(user=request.user)
            messages.success(request, f"Reversal entry {reversal.reference} created and posted.")
            return redirect("accounting:journal_entry_detail", pk=reversal.pk)
        except Exception as e:
            messages.error(request, str(e))
    return redirect("accounting:journal_entry_detail", pk=pk)


class TrialBalanceView(LoginRequiredMixin, TemplateView):
    template_name = "accounting/trial_balance.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Trial Balance"
        company = self.request.active_company
        if not company:
            return ctx
        accounts = Account.objects.filter(company=company, is_active=True).order_by("code")
        rows = []
        total_dr = Decimal("0")
        total_cr = Decimal("0")
        for acc in accounts:
            lines = acc.journal_lines.filter(journal_entry__status="POSTED")
            dr = lines.aggregate(t=Sum("debit_amount"))["t"] or Decimal("0")
            cr = lines.aggregate(t=Sum("credit_amount"))["t"] or Decimal("0")
            if dr > 0 or cr > 0:
                rows.append({"account": acc, "debit": dr, "credit": cr})
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
        from datetime import date
        ctx = super().get_context_data(**kwargs)
        account = get_object_or_404(
            Account, pk=kwargs["pk"], company=self.request.active_company,
        )
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
                "description": ln.description or ln.journal_entry.narration,
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
