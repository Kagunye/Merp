"""The ledger feeds the bank book.

A bank statement is reconciled against the bank book — the BankTransaction
rows of one bank account — and the bank book has to hold every movement the
ledger says went through that account. Money reaches a bank or cash ledger
account from all over the system: a customer receipt, a supplier payment, the
till's card and M-Pesa takings, a payroll run's net pay, an expense claim, a
transfer, a hand-typed journal. If only the lines typed on the banking screen
were in the bank book, none of those could ever be ticked on a statement, and
a perfectly kept account would never reconcile.

Mechanism
---------
A ``post_save`` receiver on ``JournalEntry`` (connected in ``BankingConfig.
ready``) runs whenever an entry is saved as POSTED with its status or its
``reversed_by`` link among the fields written — which is exactly what
``JournalEntry.post`` and ``JournalEntry.reverse`` write. A signal rather than
an edit to ``post``/``reverse`` keeps the bank book's rules here, beside the
bank book, and catches every module that posts or reverses an entry without
any of them having to know banking exists.

* When an entry posts, ``feed`` nets its lines per ledger account, and for
  each ledger account a bank account maps to it adds one LEDGER line to that
  bank account — inflow for a net debit, outflow for a net credit — linked to
  the entry. A bank account that already has a line linked to the entry is
  left alone: that is how a manual deposit or a transfer, which link their
  own lines to their entry before posting it, never get a second line. So
  ``feed`` is idempotent — running it twice adds nothing.

* When an entry is reversed, ``unwind`` decides per bank account. If the
  original's line has not been ticked on any statement, the bank never saw
  the money, so the original's LEDGER line and the reversal's line both go —
  the statement screen should not offer two lines that cancel out. If it has
  been ticked, the bank did see it: the line stays where it is on its
  statement, and the reversal keeps (or gets) a line of its own to tick when
  the money comes back. Also idempotent.

Entries that are not money moving are not fed: an OPENING entry (the bank
account's own opening balance stands for that), a period or year CLOSING, and
an exchange REVALUATION (it restates the shilling value of dollars the bank
still holds; the statement does not move).
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Sum

ZERO = Decimal("0")
CENT = Decimal("0.01")

NOT_MONEY = ("OPENING", "CLOSING", "REVALUATION")


def _bank_accounts_for(company, account_ids):
    """The bank account behind each ledger account, active ones first."""
    from .models import BankAccount

    found = {}
    for bank in (BankAccount.objects.filter(company=company, gl_account_id__in=account_ids)
                 .select_related("company").order_by("-is_active", "created_at")):
        found.setdefault(bank.gl_account_id, bank)
    return found


def _net_by_account(entry):
    """Debits less credits on each ledger account the entry touches."""
    rows = entry.lines.values("account_id").annotate(
        dr=Sum("debit_amount"), cr=Sum("credit_amount"))
    return {row["account_id"]: (row["dr"] or ZERO) - (row["cr"] or ZERO) for row in rows}


def _own_money(entry, bank, base):
    """A book-currency amount in the bank account's own currency, or None.

    A shilling account takes the ledger figure as it stands. A dollar account's
    statement prints dollars: the entry's own rate is used when the entry was
    translated from that currency (a dollar receipt), otherwise the filed rate
    for the day. With neither, there is no honest dollar figure to put on the
    line and it is left out — the transfer and bank screens, which take the
    dollar amount from the bookkeeper, are the way to record it.
    """
    from apps.accounting.models import ExchangeRate
    from django.core.exceptions import ValidationError

    book = (bank.company.currency or "KES").upper()
    code = (bank.currency or book).upper()
    if code == book:
        return base
    if (entry.source_currency or "").upper() == code and entry.exchange_rate:
        rate = entry.exchange_rate
    else:
        try:
            rate = ExchangeRate.rate_for(bank.company, code, entry.posting_date)
        except ValidationError:
            return None
    if not rate:
        return None
    return (base / Decimal(str(rate))).quantize(CENT, rounding=ROUND_HALF_UP)


def _line_for(entry, bank, net, description=None):
    """Write the bank-book line one entry puts on one bank account."""
    from .models import BankTransaction

    amount = _own_money(entry, bank, abs(net))
    if not amount:
        return None
    return BankTransaction.objects.create(
        bank_account=bank,
        transaction_date=entry.posting_date,
        transaction_type="CREDIT" if net > 0 else "DEBIT",
        amount=amount,
        description=(description or entry.notes or entry.reference)[:500],
        reference=entry.reference[:200],
        origin="LEDGER",
        journal_entry=entry,
    )


def _seen(line):
    """Has a statement — posted or still a draft — ticked this line?"""
    return line.is_reconciled or line.reconciliation_id is not None


def feed(entry):
    """Put a posted entry's bank and cash movements on the bank book.

    Returns the lines added (an empty list when there was nothing to add).
    """
    from .models import BankTransaction

    if entry.status != "POSTED" or entry.entry_type in NOT_MONEY:
        return []
    if entry.reversed_by_id or entry.is_reversal:
        # A reversed entry, and a reversal, are answered by unwind.
        return []
    nets = _net_by_account(entry)
    banks = _bank_accounts_for(entry.company, [pk for pk, net in nets.items() if net])
    added = []
    for account_id, bank in banks.items():
        if BankTransaction.objects.filter(journal_entry=entry, bank_account=bank).exists():
            continue
        line = _line_for(entry, bank, nets[account_id])
        if line is not None:
            added.append(line)
    return added


def unwind(entry):
    """Answer the bank book when a posted entry has been reversed.

    Per bank account: unseen by the bank, the original's mirror line and the
    reversal's line are both removed; seen, the original stays on its
    statement and the reversal has a line of its own for the next one.
    """
    from .models import BankTransaction

    reversal = entry.reversed_by
    if reversal is None or entry.entry_type in NOT_MONEY:
        return
    nets = _net_by_account(entry)
    reversal_nets = _net_by_account(reversal)
    touched = set(BankTransaction.objects.filter(journal_entry__in=[entry, reversal])
                  .values_list("bank_account_id", flat=True))
    banks = _bank_accounts_for(entry.company, [pk for pk, net in nets.items() if net])
    for bank in banks.values():
        touched.add(bank.pk)
    for bank_id in touched:
        original = list(BankTransaction.objects.filter(journal_entry=entry, bank_account_id=bank_id))
        mirror = BankTransaction.objects.filter(journal_entry=reversal, bank_account_id=bank_id)
        # A manual or transfer line still linked to its entry belongs to the
        # bank screen, which unlinks it before reversing; one left linked has
        # been reversed from elsewhere, so the bank book keeps both sides and
        # still agrees with the ledger.
        keep = any(_seen(line) or line.origin != "LEDGER" for line in original)
        if keep:
            if not mirror.exists():
                bank = next((b for b in banks.values() if b.pk == bank_id), None) or \
                    original[0].bank_account
                net = reversal_nets.get(bank.gl_account_id, ZERO)
                if net:
                    _line_for(reversal, bank, net, description=f"Reversal of {entry.reference}")
            continue
        BankTransaction.objects.filter(pk__in=[line.pk for line in original]).delete()
        mirror.filter(is_reconciled=False, reconciliation__isnull=True).delete()


def on_entry_saved(sender, instance, created=False, update_fields=None, raw=False, **kwargs):
    """post_save receiver for JournalEntry; see the module docstring."""
    if raw or instance.status != "POSTED":
        return
    if update_fields is not None and not ({"status", "reversed_by"} & set(update_fields)):
        return
    if instance.reversed_by_id:
        unwind(instance)
    else:
        feed(instance)
