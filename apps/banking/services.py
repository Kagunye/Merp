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


def latest_posted(bank_account, exclude=None):
    """The newest posted statement on this account, or None."""
    found = Reconciliation.objects.filter(bank_account=bank_account, status="POSTED")
    if exclude is not None:
        found = found.exclude(pk=exclude.pk)
    return found.order_by("-statement_date", "-created_at").first()


def refuse_backdating(bank_account, statement_date, exclude=None):
    """Statements run forward only.

    Each statement opens on the closing balance of the last posted one before
    it. One dated beneath a statement already posted would open on a balance
    from before that one, take items the later statement has already
    accounted for between them, and leave the later statement opening on a
    figure that no longer follows from anything. The bank prints statements
    in order; they are filed in order.
    """
    last = latest_posted(bank_account, exclude=exclude)
    if last and statement_date < last.statement_date:
        raise ValidationError(
            f"{bank_account.name} is already reconciled to {last.statement_date:%d %b %Y} on "
            f"statement {last.reference}, so a statement to {statement_date:%d %b %Y} would sit "
            f"beneath it. Statements run forward only — reverse {last.reference} first if the "
            "earlier statement really was missed.")


def refresh_opening(reconciliation, save=True):
    """Bring a draft's opening balance up to the last posted statement.

    A draft takes its opening balance when it is started, from whatever was
    posted then. Post an earlier statement afterwards and the draft would go
    on opening from the account's original balance, out by everything the
    earlier statement cleared. A draft is therefore re-opened from the latest
    posted statement every time it is shown or posted; a posted or reversed
    statement keeps the figure it was filed with.

    Returns the reconciliation. With save=False the figure is only set on the
    object (for a page that is merely being looked at).
    """
    if reconciliation.status != "DRAFT":
        return reconciliation
    opening = carried_forward_balance(reconciliation.bank_account, reconciliation.statement_date)
    if opening != reconciliation.opening_balance:
        reconciliation.opening_balance = opening
        if save:
            reconciliation.save(update_fields=["opening_balance", "updated_at"])
    return reconciliation


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
    """Uncleared transactions up to the statement date -- what gets ticked.

    Only lines dated on or before the statement date, and never after today:
    a receipt or payment dated next week has not reached the bank, so it
    cannot be on any statement yet. Those are listed apart (see future_lines)
    rather than offered for ticking.
    """
    statement_date = min(statement_date, timezone.localdate())
    return BankTransaction.objects.filter(
        bank_account=bank_account,
        is_reconciled=False,
        transaction_date__lte=statement_date,
    ).order_by("transaction_date", "created_at")


def future_lines(bank_account):
    """Lines dated after today: shown on their own, never ticked or counted."""
    return BankTransaction.objects.filter(
        bank_account=bank_account, transaction_date__gt=timezone.localdate(),
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
    refuse_backdating(bank_account, statement_date)
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


def update_draft(reconciliation, *, statement_date=None, closing_balance=None, notes=None):
    """Correct a draft statement's header: its date, closing balance or notes.

    A draft is the bookkeeper's working paper — a closing balance typed with a
    digit wrong, or the wrong month's date, has to be fixable without
    reversing anything, because nothing has been filed yet. The same rules as
    starting one apply: not in the future, not on a date another statement
    already has, not beneath a posted statement. An item already ticked on it
    that the new date would put after the statement has to be unticked first,
    so the statement never holds something it could not have printed. The
    opening balance follows the new date.
    """
    ensure_editable(reconciliation)
    bank = reconciliation.bank_account
    if statement_date is not None and statement_date != reconciliation.statement_date:
        if statement_date > timezone.localdate():
            raise ValidationError("A statement cannot be dated in the future.")
        clash = Reconciliation.objects.filter(
            bank_account=bank, statement_date=statement_date,
        ).exclude(status="REVERSED").exclude(pk=reconciliation.pk).first()
        if clash:
            raise ValidationError(
                f"{bank.name} already has statement {clash.reference} to "
                f"{statement_date:%d %b %Y}. Pick another date, or reverse that one first.")
        refuse_backdating(bank, statement_date, exclude=reconciliation)
        late = reconciliation.transactions.filter(transaction_date__gt=statement_date).count()
        if late:
            raise ValidationError(
                f"{late} item(s) ticked on this statement are dated after "
                f"{statement_date:%d %b %Y}. Untick them first, then change the date.")
        reconciliation.statement_date = statement_date
    if closing_balance is not None:
        reconciliation.closing_balance = closing_balance
    if notes is not None:
        reconciliation.notes = notes
    reconciliation.save(update_fields=["statement_date", "closing_balance", "notes",
                                       "updated_at"])
    return refresh_opening(reconciliation)


@transaction.atomic
def delete_draft(reconciliation):
    """Throw away a draft statement, releasing whatever was ticked on it.

    Only a draft: a posted statement is a filed record and is reversed, which
    keeps it on file marked as such. A draft was never more than a working
    paper, so deleting it leaves nothing behind but its items, outstanding
    again for the next statement.
    """
    ensure_editable(reconciliation)
    released = 0
    for txn in reconciliation.transactions.all():
        txn.reconciliation = None
        txn.is_reconciled = False
        txn.save(update_fields=["reconciliation", "is_reconciled", "updated_at"])
        released += 1
    reconciliation.delete()
    return released


def post(reconciliation, user=None, notes=None):
    """Lock the statement once it ties to the bank's closing balance.

    `notes`, when given, are saved on the draft first: they are how a
    statement that is out of balance is posted anyway, so the bookkeeper can
    write down why on the posting screen itself rather than having had to
    know at the start.
    """
    if reconciliation.status != "DRAFT":
        raise ValidationError(f"Only a draft can be posted (this one is {reconciliation.status}).")
    later = (
        Reconciliation.objects.filter(
            bank_account=reconciliation.bank_account, status="POSTED",
            statement_date__gt=reconciliation.statement_date,
        ).order_by("-statement_date").first()
    )
    if later:
        raise ValidationError(
            f"Statement {later.reference} to {later.statement_date:%d %b %Y} is already posted, "
            f"and this one is dated before it. Statements run forward only — reverse "
            f"{later.reference} first if this one really was missed.")
    if notes is not None and notes.strip() and notes.strip() != (reconciliation.notes or "").strip():
        reconciliation.notes = notes.strip()
        reconciliation.save(update_fields=["notes", "updated_at"])
    refresh_opening(reconciliation)
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
