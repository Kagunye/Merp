"""Bank reconciliation: clearing book transactions against a bank statement."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import BankAccount, BankTransaction, Reconciliation

ZERO = Decimal("0")


def carried_forward_balance(bank_account, statement_date):
    """Opening balance for a new statement.

    The closing balance of the last posted statement before this date, so a
    second reconciliation starts where the first one left rather than from the
    account's original opening balance.
    """
    last = (
        Reconciliation.objects.filter(
            bank_account=bank_account,
            status="POSTED",
            statement_date__lt=statement_date,
        ).order_by("-statement_date").first()
    )
    return last.closing_balance if last else bank_account.opening_balance


def gl_balance_at(account, as_of):
    """Balance of a GL account up to and including `as_of`, signed normally."""
    from apps.accounting.models import NORMAL_BALANCE

    lines = account.journal_lines.filter(
        journal_entry__status="POSTED", journal_entry__posting_date__lte=as_of
    )
    debit = lines.aggregate(t=Sum("debit_amount"))["t"] or ZERO
    credit = lines.aggregate(t=Sum("credit_amount"))["t"] or ZERO
    if NORMAL_BALANCE.get(account.account_type) == "DEBIT":
        return debit - credit
    return credit - debit


def outstanding(bank_account, statement_date):
    """Uncleared transactions up to the statement date -- what gets ticked."""
    return BankTransaction.objects.filter(
        bank_account=bank_account,
        is_reconciled=False,
        transaction_date__lte=statement_date,
    ).order_by("transaction_date", "created_at")


def reconcilable_accounts(company):
    """Bank accounts whose ledger account is expected to be reconciled."""
    if company is None:
        return BankAccount.objects.none()
    return BankAccount.objects.filter(company=company, is_active=True).select_related("gl_account")


def create_draft(company, bank_account, reference, statement_date, closing_balance,
                 user=None, opening_balance=None):
    if Reconciliation.objects.filter(
        bank_account=bank_account, statement_date=statement_date
    ).exclude(status="REVERSED").exists():
        raise ValidationError(
            f"{bank_account.name} is already reconciled to {statement_date:%d %b %Y} "
            "(or has a draft waiting). Reverse it first if the statement is wrong."
        )
    return Reconciliation.objects.create(
        company=company,
        bank_account=bank_account,
        reference=reference,
        statement_date=statement_date,
        opening_balance=(
            carried_forward_balance(bank_account, statement_date)
            if opening_balance is None else opening_balance
        ),
        closing_balance=closing_balance,
        reconciled_by=user,
    )


def clear(txn, reconciliation):
    """Attach a transaction to a statement and mark it cleared.

    Current clearing state is read back from the database rather than trusted
    from the instance handed in: a caller holding a stale object must not be
    able to double-clear a transaction, or clear one the statement has locked.
    """
    ensure_editable(reconciliation)
    if txn.bank_account_id != reconciliation.bank_account_id:
        raise ValidationError("That transaction belongs to a different bank account.")
    if txn.transaction_date > reconciliation.statement_date:
        raise ValidationError(
            f"{txn.transaction_date:%d %b %Y} is after the statement date "
            f"{reconciliation.statement_date:%d %b %Y}, so it cannot be on this statement."
        )
    current = (
        BankTransaction.objects.filter(pk=txn.pk)
        .select_related("reconciliation").first()
    )
    if current and current.is_reconciled:
        raise ValidationError(
            f"Already cleared on statement {current.reconciliation.reference}."
            if current.reconciliation else "Already cleared."
        )
    txn.reconciliation = reconciliation
    txn.is_reconciled = True
    txn.save(update_fields=["reconciliation", "is_reconciled", "updated_at"])
    return txn


def unclear(txn):
    """Release a transaction back to the outstanding list."""
    current = (
        BankTransaction.objects.filter(pk=txn.pk)
        .select_related("reconciliation").first()
    )
    if current and current.reconciliation_id:
        ensure_editable(current.reconciliation)
    txn.reconciliation = None
    txn.is_reconciled = False
    txn.save(update_fields=["reconciliation", "is_reconciled", "updated_at"])
    return txn


def post(reconciliation, user=None):
    """Lock the statement once it ties to the bank's closing balance."""
    if reconciliation.status != "DRAFT":
        raise ValidationError(f"Only a draft can be posted (this one is {reconciliation.status}).")
    if not reconciliation.is_balanced and not reconciliation.notes.strip():
        raise ValidationError(
            f"Out of balance by {reconciliation.difference} -- clear the missing items, "
            "correct the closing balance, or record why the difference is accepted."
        )
    reconciliation.status = "POSTED"
    reconciliation.reconciled_by = user
    reconciliation.reconciled_at = timezone.now()
    reconciliation.save(update_fields=["status", "reconciled_by", "reconciled_at", "updated_at"])
    return reconciliation


def reverse(reconciliation):
    """Undo a posted statement and release every transaction it cleared.

    Statements form a chain: each one opens on the closing balance of the
    last posted statement before it. Reversing an earlier statement while a
    later one stays posted would pull a link out of the middle of that chain
    — the later statement would still open on a balance nobody now stands
    behind, and a fresh draft for the earlier date would open from the
    account's original opening balance, skipping everything in between. So
    the statements are unwound from the newest back: reverse the later one
    first, then this one.
    """
    if reconciliation.status != "POSTED":
        raise ValidationError("Only a posted statement can be reversed.")
    later = (
        Reconciliation.objects.filter(
            bank_account=reconciliation.bank_account, status="POSTED",
            statement_date__gt=reconciliation.statement_date,
        ).order_by("-statement_date").first()
    )
    if later:
        raise ValidationError(
            f"Statement {later.reference} to {later.statement_date:%d %b %Y} is posted after "
            f"this one and opens on its closing balance. Reverse {later.reference} first, "
            f"then {reconciliation.reference}.")
    with transaction.atomic():
        for txn in reconciliation.transactions.all():
            txn.reconciliation = None
            txn.is_reconciled = False
            txn.save(update_fields=["reconciliation", "is_reconciled", "updated_at"])
        reconciliation.status = "REVERSED"
        reconciliation.save(update_fields=["status", "updated_at"])
    return reconciliation


def ensure_editable(reconciliation):
    """Only a draft statement may gain or lose items."""
    if reconciliation.status != "DRAFT":
        raise ValidationError(
            f"Statement {reconciliation.reference} is {reconciliation.status.lower()} "
            "and can no longer be changed. Reverse it to reopen."
        )
