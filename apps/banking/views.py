"""Banking views: accounts, transactions and statement reconciliation."""
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_POST
from django.views.generic import (
    CreateView, DeleteView, DetailView, FormView, ListView, TemplateView, UpdateView,
)

from apps.accounting import gl_service
from apps.accounting.currency import book_currency
from apps.core.mixins import CompanyRequiredMixin

from . import services
from .forms import (
    BankAccountForm, BankReconciliationForm, BankTransactionForm, BankTransferForm,
)
from .models import BankAccount, BankTransaction, Reconciliation


def _scoped_account(company, pk):
    """Find a bank account by primary key without exploding on a bad id."""
    if not company or not pk:
        return None
    return next(
        (a for a in BankAccount.objects.filter(company=company) if str(a.pk) == str(pk)), None
    )


class ModuleHomeView(LoginRequiredMixin, TemplateView):
    template_name = "banking/home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Banking"
        company = getattr(self.request, "active_company", None)
        if company:
            ctx["accounts"] = BankAccount.objects.filter(company=company)
            ctx["recent_transactions"] = BankTransaction.objects.filter(
                bank_account__company=company
            ).select_related("bank_account")[:10]
            ctx["unreconciled_count"] = BankTransaction.objects.filter(
                bank_account__company=company, is_reconciled=False
            ).count()
        return ctx


class BankAccountListView(LoginRequiredMixin, ListView):
    model = BankAccount
    template_name = "banking/account_list.html"
    context_object_name = "accounts"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return BankAccount.objects.none()
        return BankAccount.objects.filter(company=company).select_related("gl_account", "company")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # The ledger balance is in the book's currency whatever the account's.
        ctx["book"] = book_currency(self.request.active_company)
        return ctx


class BankAccountCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    model = BankAccount
    form_class = BankAccountForm
    template_name = "banking/account_form.html"
    success_url = reverse_lazy("banking:account_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        messages.success(self.request, "Bank account created.")
        return super().form_valid(form)


class BankAccountUpdateView(LoginRequiredMixin, UpdateView):
    model = BankAccount
    form_class = BankAccountForm
    template_name = "banking/account_form.html"
    success_url = reverse_lazy("banking:account_list")

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return BankAccount.objects.none()
        return BankAccount.objects.filter(company=company)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs


class BankAccountDetailView(LoginRequiredMixin, DetailView):
    model = BankAccount
    template_name = "banking/account_detail.html"
    context_object_name = "account"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return BankAccount.objects.none()
        return BankAccount.objects.filter(company=company).select_related("gl_account")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["transactions"] = self.object.transactions.select_related("reconciliation")[:50]
        ctx["book"] = book_currency(self.request.active_company)
        ctx["reconciliations"] = self.object.reconciliations.all()[:10]
        ctx["unreconciled"] = self.object.transactions.filter(is_reconciled=False).count()
        return ctx


class BankTransactionListView(LoginRequiredMixin, ListView):
    model = BankTransaction
    template_name = "banking/transaction_list.html"
    context_object_name = "transactions"
    paginate_by = 50

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return BankTransaction.objects.none()
        qs = BankTransaction.objects.filter(
            bank_account__company=company
        ).select_related("bank_account", "reconciliation")

        reconciled = self.request.GET.get("reconciled")
        if reconciled in ("true", "false"):
            qs = qs.filter(is_reconciled=(reconciled == "true"))

        account = _scoped_account(company, self.request.GET.get("account"))
        if account:
            qs = qs.filter(bank_account=account)

        q = self.request.GET.get("q")
        if q:
            qs = qs.filter(Q(description__icontains=q) | Q(reference__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        ctx["bank_accounts"] = BankAccount.objects.filter(company=company, is_active=True) if company else BankAccount.objects.none()
        ctx["selected_account"] = self.request.GET.get("account", "")
        ctx["reconciled_filter"] = self.request.GET.get("reconciled", "")
        ctx["q"] = self.request.GET.get("q", "")
        ctx["shown_debit"] = sum((t.amount for t in ctx["object_list"] if t.transaction_type == "DEBIT"), Decimal("0"))
        ctx["shown_credit"] = sum((t.amount for t in ctx["object_list"] if t.transaction_type == "CREDIT"), Decimal("0"))
        ctx["shown_net"] = sum((t.signed_amount for t in ctx["object_list"]), Decimal("0"))
        return ctx


_TYPE_ALIASES = {"DEPOSIT": "CREDIT", "WITHDRAWAL": "DEBIT"}


class BankTransactionCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    model = BankTransaction
    form_class = BankTransactionForm
    template_name = "banking/transaction_form.html"
    success_url = reverse_lazy("banking:transaction_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        raw = (self.request.GET.get("type") or "").upper()
        direction = _TYPE_ALIASES.get(raw, raw if raw in ("CREDIT", "DEBIT") else "")
        if direction:
            initial["transaction_type"] = direction
        account = _scoped_account(self.request.active_company, self.request.GET.get("account"))
        if account:
            initial["bank_account"] = account.pk
        return initial

    def form_valid(self, form):
        """Save the line and book its ledger entry, as one step.

        Either both land or neither does: a line without its entry is the
        bank book disagreeing with the ledger, which is the bug this screen
        used to have.
        """
        try:
            with db_transaction.atomic():
                txn = form.save(commit=False)
                txn.origin = "MANUAL"
                txn.save()
                entry = gl_service.post_bank_transaction(txn, user=self.request.user)
        except ValidationError as e:
            form.add_error(None, e.messages[0])
            return self.form_invalid(form)
        self.object = txn
        messages.success(self.request, f"Transaction recorded and posted to the ledger in "
                                       f"{entry.reference}.")
        return redirect(self.get_success_url())


# The fields whose change means the ledger entry behind a manual line is wrong
# and has to be booked again. A new description or reference is only words.
_LEDGER_FIELDS = ("bank_account_id", "transaction_date", "transaction_type", "amount",
                  "counter_account_id")


def _not_ours_to_change(txn):
    """Why the bank screen may not edit or delete this line itself, or "".

    A line fed from another document's entry is corrected at that document —
    the receipt, the bill, the till, the payroll run — whose own reversal
    takes the line off the bank book. A transfer's two sides go together.
    """
    if txn.origin == "LEDGER":
        ref = txn.journal_entry.reference if txn.journal_entry else "its ledger entry"
        return (f"This line comes from {ref}, posted by another screen. Correct or reverse "
                "that document there — reversing it takes the line off the bank book.")
    return ""


class BankTransactionUpdateView(LoginRequiredMixin, UpdateView):
    """Correct a transaction that has not been cleared on a statement yet.

    A cleared transaction is evidence for the statement it sits on: changing its
    amount, date, direction or account would leave a posted statement that no
    longer adds up to the balance the bank printed, while still marked
    reconciled. Like delete, an edit is refused until it is released — untick
    it on a draft statement, or reverse the posted one.
    """
    model = BankTransaction
    form_class = BankTransactionForm
    template_name = "banking/transaction_form.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            txn = self.get_object()
            if txn.is_reconciled or txn.reconciliation_id:
                recon = txn.reconciliation
                statement = recon.reference if recon else "a posted statement"
                how = ("untick it on that draft statement" if recon and recon.status == "DRAFT"
                       else "reverse the statement")
                messages.error(
                    request,
                    f"That transaction is cleared on statement {statement}; "
                    f"{how} before changing it.",
                )
                return redirect("banking:transaction_detail", pk=txn.pk)
            refusal = _not_ours_to_change(txn)
            if not refusal and txn.origin == "TRANSFER":
                refusal = ("This line is one side of a transfer. Delete the transfer and enter "
                           "it again, so both accounts change together.")
            if refusal:
                messages.error(request, refusal)
                return redirect("banking:transaction_detail", pk=txn.pk)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return BankTransaction.objects.none()
        return BankTransaction.objects.filter(bank_account__company=company)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_success_url(self):
        return reverse("banking:transaction_detail", args=[self.object.pk])

    def form_valid(self, form):
        """Save the correction and, if it moves money, re-book the entry.

        The old entry is reversed on its own date and a fresh one booked from
        the corrected line, so the ledger ends up saying what the line now
        says while still showing that it once said something else.
        """
        before = BankTransaction.objects.select_related("journal_entry").get(pk=self.object.pk)
        try:
            with db_transaction.atomic():
                txn = form.save(commit=False)
                rebook = (before.journal_entry_id is None
                          or any(getattr(before, f) != getattr(txn, f) for f in _LEDGER_FIELDS))
                if rebook:
                    gl_service.unbook_bank_transaction(before, user=self.request.user)
                    txn.journal_entry = None
                txn.origin = "MANUAL"
                txn.save()
                if rebook:
                    gl_service.post_bank_transaction(txn, user=self.request.user)
        except ValidationError as e:
            form.add_error(None, e.messages[0])
            return self.form_invalid(form)
        self.object = txn
        messages.success(
            self.request,
            f"Transaction updated and re-posted in {txn.journal_entry.reference}." if rebook
            else "Transaction updated.")
        return redirect(self.get_success_url())


class BankTransactionDeleteView(LoginRequiredMixin, DeleteView):
    model = BankTransaction
    template_name = "banking/transaction_confirm_delete.html"
    context_object_name = "txn"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return BankTransaction.objects.none()
        return BankTransaction.objects.filter(bank_account__company=company).select_related("bank_account")

    def get_success_url(self):
        return reverse("banking:transaction_list")

    def refuse_if_cleared(self):
        """A transaction inside a filed statement is evidence for that filing,
        so it cannot be deleted until the statement is reversed. A line fed
        from another document's entry is removed by reversing that document."""
        if self.object.is_reconciled:
            statement = (self.object.reconciliation.reference if self.object.reconciliation
                         else "a posted statement")
            messages.error(
                self.request,
                f"That transaction is cleared on statement {statement}; "
                "reverse the statement before deleting it.",
            )
            return redirect("banking:transaction_detail", pk=self.object.pk)
        refusal = _not_ours_to_change(self.object)
        if refusal:
            messages.error(self.request, refusal)
            return redirect("banking:transaction_detail", pk=self.object.pk)
        return None

    def remove(self):
        """Delete the line and take its entry off the ledger with it.

        The entry is reversed, not erased; a transfer loses both its sides.
        """
        txn = self.object
        try:
            with db_transaction.atomic():
                if txn.origin == "TRANSFER" and txn.journal_entry_id:
                    reversal = gl_service.unbook_bank_transfer(
                        txn.journal_entry, user=self.request.user)
                    what = "Transfer deleted from both accounts"
                else:
                    reversal = gl_service.unbook_bank_transaction(txn, user=self.request.user)
                    txn.delete()
                    what = "Transaction deleted"
        except ValidationError as e:
            messages.error(self.request, e.messages[0])
            return redirect("banking:transaction_detail", pk=txn.pk)
        messages.success(
            self.request,
            f"{what}; its ledger entry is reversed by {reversal.reference}." if reversal
            else f"{what}.")
        return redirect(self.get_success_url())

    def form_valid(self, form):
        # Django 5 routes the confirmation POST through form_valid, not delete.
        blocked = self.refuse_if_cleared()
        if blocked is not None:
            return blocked
        return self.remove()

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        blocked = self.refuse_if_cleared()
        if blocked is not None:
            return blocked
        return self.remove()


class BankTransferView(LoginRequiredMixin, CompanyRequiredMixin, FormView):
    """Move money between two of the company's own bank or cash accounts.

    One ledger entry carries both sides — and, between two currencies, the
    realised exchange on the bank's rate against the filed one — while each
    account gets its own line on its bank book to tick on its statement.
    """
    form_class = BankTransferForm
    template_name = "banking/transfer_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_initial(self):
        from django.utils import timezone
        initial = super().get_initial()
        initial["transfer_date"] = timezone.localdate()
        account = _scoped_account(self.request.active_company, self.request.GET.get("account"))
        if account:
            initial["from_account"] = account.pk
        return initial

    def form_valid(self, form):
        data = form.cleaned_data
        try:
            entry, out_line, in_line = gl_service.post_bank_transfer(
                data["from_account"], data["to_account"], data["amount_sent"],
                data["transfer_date"], amount_received=data.get("amount_received"),
                reference=data.get("reference") or "",
                description=data.get("description") or "", user=self.request.user)
        except ValidationError as e:
            form.add_error(None, e.messages[0])
            return self.form_invalid(form)
        messages.success(
            self.request,
            f"Transfer posted in {entry.reference}: {out_line.bank_account.currency} "
            f"{out_line.amount} out of {out_line.bank_account.name}, "
            f"{in_line.bank_account.currency} {in_line.amount} into {in_line.bank_account.name}.")
        return redirect(reverse("banking:transaction_list") + f"?account={out_line.bank_account.pk}")


class BankTransactionDetailView(LoginRequiredMixin, DetailView):
    model = BankTransaction
    template_name = "banking/transaction_detail.html"
    context_object_name = "txn"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return BankTransaction.objects.none()
        return BankTransaction.objects.filter(
            bank_account__company=company
        ).select_related("bank_account", "reconciliation", "journal_entry")


# ------------------------- Statement reconciliation -------------------------


class ReconciliationListView(LoginRequiredMixin, TemplateView):
    """Every statement filed for this company, plus what is still waiting to
    be cleared on each bank account."""
    template_name = "banking/reconciliation_list.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if not company:
            return ctx
        ctx["reconciliations"] = Reconciliation.objects.filter(
            company=company
        ).select_related("bank_account", "reconciled_by")

        rows = []
        for account in BankAccount.objects.filter(company=company, is_active=True).select_related("gl_account"):
            pending = services.outstanding(account, date.today())
            last = account.reconciliations.exclude(status="REVERSED").order_by("-statement_date").first()
            rows.append({
                "account": account,
                "pending_count": pending.count(),
                "pending_value": sum((t.signed_amount for t in pending), Decimal("0")),
                "last": last,
            })
        ctx["account_rows"] = rows
        ctx["drafts"] = Reconciliation.objects.filter(company=company, status="DRAFT").count()
        return ctx


class ReconciliationCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    model = Reconciliation
    form_class = BankReconciliationForm
    template_name = "banking/reconciliation_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        account = _scoped_account(self.request.active_company, self.request.GET.get("account"))
        if account:
            initial["bank_account"] = account.pk
        return initial

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        account = _scoped_account(self.request.active_company, self.request.GET.get("account"))
        if account:
            pending = services.outstanding(account, date.today())
            ctx["preview_account"] = account
            ctx["preview_items"] = pending
            ctx["preview_value"] = sum((t.signed_amount for t in pending), Decimal("0"))
            ctx["preview_opening"] = services.carried_forward_balance(account, date.today())
        return ctx

    def form_valid(self, form):
        company = self.request.active_company
        try:
            with db_transaction.atomic():
                recon = services.create_draft(
                    company=company,
                    bank_account=form.cleaned_data["bank_account"],
                    reference=form.cleaned_data["reference"],
                    statement_date=form.cleaned_data["statement_date"],
                    closing_balance=form.cleaned_data["closing_balance"],
                    user=self.request.user,
                )
                recon.notes = form.cleaned_data.get("notes", "")
                recon.save(update_fields=["notes", "updated_at"])
        except ValidationError as e:
            messages.error(self.request, e.messages[0])
            return self.render_to_response(self.get_context_data(form=form))
        messages.success(
            self.request,
            f"Statement {recon.reference} started. Tick the items that appear on it, then post.",
        )
        self.object = recon
        return redirect("banking:reconciliation_detail", pk=recon.pk)

    def get_success_url(self):
        return reverse("banking:reconciliation_detail", args=[self.object.pk])


class ReconciliationDetailView(LoginRequiredMixin, DetailView):
    """The reconciliation screen: statement figures on one side, the book
    transactions you can clear against them on the other."""
    model = Reconciliation
    template_name = "banking/reconciliation_detail.html"
    context_object_name = "reconciliation"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Reconciliation.objects.none()
        return Reconciliation.objects.filter(company=company).select_related("bank_account", "reconciled_by")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        recon = self.object
        bank = recon.bank_account
        ctx["cleared"] = recon.transactions.order_by("transaction_date", "created_at")
        ctx["pending"] = services.outstanding(bank, recon.statement_date)
        ctx["after_statement"] = BankTransaction.objects.filter(
            bank_account=bank, is_reconciled=False,
            transaction_date__gt=recon.statement_date,
        ).count()
        ctx["opening"] = recon.opening_balance
        ctx["cleared_movement"] = recon.cleared_movement
        ctx["computed"] = recon.computed_balance
        ctx["difference"] = recon.difference
        ctx["is_balanced"] = recon.is_balanced
        ctx["books_balance"] = services.gl_balance_at(bank.gl_account, recon.statement_date)
        ctx["currency"] = bank.currency
        # The ledger figure is always in the book's own currency.
        ctx["book"] = book_currency(self.request.active_company)
        ctx["statement_label"] = (
            f"{bank.name} · statement to {recon.statement_date:%d %b %Y}"
        )
        return ctx


@login_required
@require_POST
def reconciliation_clear(request, pk):
    """Apply the tick boxes: clear what is ticked, release what is not."""
    recon = get_object_or_404(
        Reconciliation, pk=pk, company=request.active_company,
    )
    wanted = set(request.POST.getlist("cleared"))
    try:
        services.ensure_editable(recon)
    except ValidationError as e:
        messages.error(request, e.messages[0])
        return redirect("banking:reconciliation_detail", pk=recon.pk)

    cleared = released = 0
    errors = []
    for txn in services.outstanding(recon.bank_account, recon.statement_date):
        if str(txn.pk) in wanted:
            try:
                services.clear(txn, recon)
                cleared += 1
            except ValidationError as e:
                errors.append(e.messages[0])
    for txn in list(recon.transactions.all()):
        if str(txn.pk) not in wanted:
            services.unclear(txn)
            released += 1

    if errors:
        for message in errors:
            messages.error(request, message)
    if cleared or released:
        messages.success(
            request,
            f"{cleared} item(s) cleared" + (f", {released} released." if released else "."),
        )
    else:
        messages.info(request, "No changes.")
    return redirect("banking:reconciliation_detail", pk=recon.pk)


@login_required
@require_POST
def reconciliation_post(request, pk):
    recon = get_object_or_404(Reconciliation, pk=pk, company=request.active_company)
    try:
        services.post(recon, user=request.user)
    except ValidationError as e:
        messages.error(request, e.messages[0])
        return redirect("banking:reconciliation_detail", pk=recon.pk)
    messages.success(
        request,
        f"Statement {recon.reference} posted — {recon.bank_account.name} is reconciled to {recon.statement_date:%d %b %Y}.",
    )
    return redirect("banking:reconciliation_detail", pk=recon.pk)


@login_required
@require_POST
def reconciliation_reverse(request, pk):
    recon = get_object_or_404(Reconciliation, pk=pk, company=request.active_company)
    released = recon.transactions.count()
    try:
        services.reverse(recon)
    except ValidationError as e:
        messages.error(request, e.messages[0])
        return redirect("banking:reconciliation_detail", pk=recon.pk)
    messages.warning(
        request,
        f"Statement {recon.reference} reversed — {released} transaction(s) are outstanding again.",
    )
    return redirect("banking:reconciliation_list")


