"""Reading a foreign balance at today's rate instead of the day it was raised.

A dollar receivable booked in March is worth 130 shillings in March, and the
ledger says so forever — that is what the booked rate is for. But a balance
sheet drawn on the last day of the month is a statement about that day, and if
the dollar now buys 135 the receivable is worth 135. Nothing has been earned or
lost in cash yet, so the difference is *unrealised*: it is posted to its own
pair of accounts, leaving the receivable and payable control accounts — and the
invoice book they are reconciled against — exactly as they were.

The book carries one revaluation at a time. Posting a new one first reverses
the one it replaces, so a period end that is redrawn cannot pile up, and a rate
that comes back round to where it started leaves the balance sheet as though
nothing had happened.

Two rules keep the unrealised figure honest:

* **A revaluation reads the paper as it stood on its own date.** What a
  document still owed on 30 September is its total less the receipts,
  payments, credit notes and debit notes dated on or before that day — not
  what it happens to owe on the day somebody gets round to running the
  revaluation. A receipt that arrived on 15 October does not shrink the
  30 September receivable, however late the September figures are drawn.

* **Settling a revalued document releases its share.** Once the money has
  come in (or gone out) the gap between the booked rate and the settlement
  rate is *realised* and booked on the payment; the unrealised share the
  revaluation was still carrying for that document would otherwise sit on
  1350/2150 and in the profit and loss beside it, counting the same rate
  move twice. So each document's share is written down when the revaluation
  is posted (RevaluationShare), and as the document is settled the share
  comes back off in step with it, dated the day of the settlement:

  - a part-payment releases its proportional slice -- 400 of a 1,000 dollar
    invoice releases 40% of the invoice's share, and the rest stays carried
    against the 600 still open;
  - settling the rest, or cancelling the document, releases whatever of the
    share is left. A cancelled document never existed as far as the balance
    sheet is concerned, so all of its share comes off;
  - a settlement keyed in late but dated before the revaluation is caught
    the same way: the share on file is what the revaluation booked, so its
    release is dated the revaluation's own day rather than lost;
  - a settlement undone (a receipt given back) reverses the releases it
    caused, and the share is carried again.

  Only the difference between what should be off and what the standing
  releases already took off is ever posted, so running the check again --
  it runs on every save of a foreign invoice or bill -- changes nothing. The
  release entries belong to the revaluation they answer: taking that
  revaluation off, or replacing it with a newer one, takes its releases off
  with it.

  The other common answer — auto-reversing every revaluation on the first day
  of the next period — was not used because this book keeps one *live*
  revaluation that the next one replaces, and the screens, the "one at a
  time" rule and the redraw of a period end are all built on that.

  The next revaluation still reads every open balance afresh: what is left
  of a part-paid invoice is valued again at the new rate, and the new
  revaluation's shares replace the old ones.
"""
import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.accounting.currency import book_currency, to_base
from apps.accounting.models import ExchangeRate, JournalEntry, period_for

# Where an unrealised difference is parked, and where it is reported.
REVALUATION = "EXCHANGE_REVALUATION"
RELEASE = "EXCHANGE_REVALUATION_RELEASE"
RECEIVABLE_ACCOUNT = "1350"
PAYABLE_ACCOUNT = "2150"
GAIN_ACCOUNT = "4320"
LOSS_ACCOUNT = "7310"

Z = Decimal("0")

# A document that answers another one is not a balance of its own.
NOT_OPEN = ["DRAFT", "CANCELLED", "CREDIT_NOTE", "DEBIT_NOTE"]


def live_revaluation(company):
    """The revaluation the book is currently sitting on, if any."""
    return (
        JournalEntry.objects.filter(
            company=company, entry_type="REVALUATION",
            source_document_type=REVALUATION, status="POSTED",
            reversed_by__isnull=True, reversal_of__isnull=True,
        ).order_by("-posting_date").first()
    )


def live_releases(company):
    """Release entries still standing against the live revaluation."""
    return JournalEntry.objects.filter(
        company=company, entry_type="REVALUATION",
        source_document_type=RELEASE, status="POSTED",
        reversed_by__isnull=True, reversal_of__isnull=True,
    )


# ───────────────────────── what was owed on the day ─────────────────────────

def _standing(entry, as_of):
    """Whether a posted entry had happened, and not been undone, by `as_of`.

    `as_of=None` asks about the book as it stands now.
    """
    if entry is None or entry.status != "POSTED":
        return False
    if as_of is not None and entry.posting_date > as_of:
        return False
    reversal = entry.reversed_by
    if reversal is None:
        return True
    return as_of is not None and reversal.posting_date > as_of


def _invoice_settlements(invoice):
    """(amount, entry) for every receipt and credit note that settled the invoice."""
    settled = []
    for allocation in invoice.allocations.select_related(
            "payment__journal_entry__reversed_by"):
        settled.append((allocation.allocated_amount, allocation.payment.journal_entry))
    for note in invoice.credit_notes.exclude(status__in=["DRAFT", "CANCELLED"]).select_related(
            "journal_entry__reversed_by"):
        settled.append((note.total_amount, note.journal_entry))
    return settled


def _bill_settlements(bill):
    """(amount, entry) for every payment and debit note that settled the bill.

    A supplier payment is a journal of its own, recorded against the bill by
    its narration; the foreign amount is read back from the payable it cleared
    at the bill's booked rate.
    """
    from apps.accounting.models import JournalEntryLine

    settled = []
    rate = bill.rate
    payments = JournalEntry.objects.filter(
        company=bill.company, entry_type="PAYMENT",
        notes=f"Supplier payment for bill {bill.reference}",
    ).select_related("reversed_by")
    for entry in payments:
        cleared = sum((line.debit_amount for line in JournalEntryLine.objects.filter(
            journal_entry=entry, account__account_type="PAYABLE")), Z)
        if cleared and rate:
            settled.append(((cleared / rate).quantize(Decimal("0.01")), entry))
    for note in bill.debit_notes.exclude(status__in=["DRAFT", "CANCELLED"]).select_related(
            "journal_entry__reversed_by"):
        settled.append((note.credited_amount or note.total_amount, note.journal_entry))
    return settled


def outstanding_at(document, as_of):
    """What a document still owed at the close of `as_of`, in its own money.

    Worked back from today's outstanding figure: every settlement that stands
    now but had not happened by `as_of` is added back, and every settlement
    that stood on `as_of` but has since been undone is taken off. Anything the
    document's own counters carry that no dated settlement explains is left
    where it is — treated as settled before the day, the way it always was.
    """
    settlements = (_bill_settlements(document) if hasattr(document, "bill_date")
                   else _invoice_settlements(document))
    owed = Decimal(str(document.outstanding_amount or 0))
    for amount, entry in settlements:
        if entry is None:
            continue
        now, then = _standing(entry, None), _standing(entry, as_of)
        if now and not then:
            owed += amount
        elif then and not now:
            owed -= amount
    return owed


def settled_on(document):
    """The day a fully settled document was settled: its latest standing settlement."""
    settlements = (_bill_settlements(document) if hasattr(document, "bill_date")
                   else _invoice_settlements(document))
    days = [entry.posting_date for _amount, entry in settlements if _standing(entry, None)]
    return max(days) if days else None


def _documents(company, model, date_field, book, as_of):
    """(document, foreign amount owed on `as_of`) for every foreign paper open that day.

    Only documents raised on or before the day being reported count: a
    receivable that did not exist on the period end cannot be revalued at it.
    A document settled since then still counts, because on the day it was
    owed.
    """
    candidates = (
        model.objects.filter(company=company, journal_entry__isnull=False,
                             **{f"{date_field}__lte": as_of})
        .exclude(status__in=NOT_OPEN)
        .exclude(currency=book)
        .select_related("journal_entry")
    )
    found = []
    for document in candidates:
        owed = outstanding_at(document, as_of)
        if owed > Z:
            found.append((document, owed))
    return found


def positions(company, as_of):
    """Every foreign balance still open at `as_of`, read at the day's rate.

    Returns one row per currency, holding the receivable side and the payable
    side, and a list of currencies that cannot be valued because no rate has
    been filed. Amounts are Decimal: foreign, carried (booked at the document's
    own rate), revalued, and the difference between the last two. Each
    document is valued on its own and the rows are the sums, so the share a
    single document contributed can be released again to the cent.
    """
    from apps.procurement.models import Bill
    from apps.sales.models import Invoice

    book = book_currency(company)
    rows = {}
    held = []

    def accumulate(documents, side):
        for document, owed in documents:
            code = document.currency.upper()
            row = rows.setdefault(code, _blank_row(code))
            row[f"{side}_foreign"] += owed
            row["documents"] += 1
            held.append((side, document, owed, code))

    accumulate(_documents(company, Invoice, "invoice_date", book, as_of), "ar")
    accumulate(_documents(company, Bill, "bill_date", book, as_of), "ap")

    missing, valued, documents = [], [], []
    rates = {}
    for code, row in sorted(rows.items()):
        try:
            rates[code] = ExchangeRate.rate_for(company, code, as_of)
        except ValidationError:
            missing.append(code)
            continue
        row["rate"] = rates[code]
        valued.append(row)
    for side, document, owed, code in held:
        if code not in rates:
            continue
        carried = to_base(owed, document.rate)
        revalued = to_base(owed, rates[code])
        row = rows[code]
        row[f"{side}_carried"] += carried
        row[f"{side}_revalued"] += revalued
        documents.append({
            "side": side, "document": document, "code": code, "foreign": owed,
            "carried": carried, "revalued": revalued, "difference": revalued - carried,
        })
    for row in valued:
        row["ar_difference"] = row["ar_revalued"] - row["ar_carried"]
        row["ap_difference"] = row["ap_revalued"] - row["ap_carried"]
        row["difference"] = row["ar_difference"] - row["ap_difference"]
    return {"as_of": as_of, "book": book, "rows": valued, "missing": missing,
            "documents": documents}


def _blank_row(code):
    return {
        "code": code, "documents": 0, "rate": None,
        "ar_foreign": Z, "ar_carried": Z, "ar_revalued": Z, "ar_difference": Z,
        "ap_foreign": Z, "ap_carried": Z, "ap_revalued": Z, "ap_difference": Z,
        "difference": Z,
    }


def summary(position):
    """The two numbers a revaluation entry is made of, in the book's money."""
    return (
        sum((row["ar_difference"] for row in position["rows"]), Z),
        sum((row["ap_difference"] for row in position["rows"]), Z),
    )


def _account(company, code, name, account_type, parent_code):
    """Find the revaluation account, making it if this book has none yet.

    Companies founded before this feature simply have no such code; refusing to
    revalue until somebody creates one by hand would punish exactly the people
    the feature is for, so the account is created on first use — the same way
    the exchange gain and loss accounts were retro-fitted.
    """
    from apps.accounting.models import Account

    account = Account.objects.filter(company=company, code=code).first()
    if account:
        return account
    parent = Account.objects.filter(company=company, code=parent_code).first()
    return Account.objects.create(
        company=company, code=code, name=name,
        account_type=account_type, parent=parent,
        description="Unrealised exchange difference, reversed at the next revaluation.",
    )


def _accounts(company):
    return {
        "gain": _account(company, GAIN_ACCOUNT, "Unrealised Exchange Gain",
                         "REVENUE", "4300"),
        "loss": _account(company, LOSS_ACCOUNT, "Unrealised Exchange Loss",
                         "EXPENSE", "7000"),
        "ar": _account(company, RECEIVABLE_ACCOUNT, "FX Revaluation — Receivables",
                       "ASSET", "1000"),
        "ap": _account(company, PAYABLE_ACCOUNT, "FX Revaluation — Payables",
                       "LIABILITY", "2000"),
    }


def take_off(entry, user=None, posting_date=None):
    """Reverse a revaluation together with every release still standing against it.

    `posting_date=None` reverses each entry on its own date (a redraw of the
    same day). Otherwise each is reversed on that date, or on its own date if
    it is later — a reversal is never dated before the entry it answers.
    """
    for release in live_releases(entry.company):
        release.reverse(
            user=user,
            posting_date=(None if posting_date is None
                          else max(posting_date, release.posting_date)))
    return entry.reverse(user=user, posting_date=posting_date)


@transaction.atomic
def post_revaluation(company, as_of, user=None):
    """Value the open foreign balances at `as_of` and book the difference.

    Returns (entry, position). The entry is None when the balances need nothing:
    either there are no open foreign documents, or the rate has not moved since
    the book was last revalued.
    """
    from apps.accounting.gl_service import _cr, _dr, _new_je

    position = positions(company, as_of)
    if position["missing"]:
        raise ValidationError(
            "Cannot revalue " + ", ".join(position["missing"]) +
            f" on {as_of:%d %b %Y}: no exchange rate has been filed for that day.")

    receivable, payable = summary(position)
    if not receivable and not payable:
        previous = live_revaluation(company)
        if previous and previous.posting_date != as_of:
            # The balances net to nothing at this date but an older revaluation
            # is still sitting on the book — it has to come off, or the balance
            # sheet keeps a difference that no longer exists.
            take_off(previous, user=user, posting_date=as_of)
        return None, position

    previous = live_revaluation(company)
    if previous:
        if previous.posting_date > as_of:
            raise ValidationError(
                f"The book is already revalued to {previous.posting_date:%d %b %Y}, "
                f"which is after {as_of:%d %b %Y}.")
        take_off(
            previous, user=user,
            # A redraw of the same period end takes the old entry out with its
            # own date, so the period nets to exactly the new figure.
            posting_date=None if previous.posting_date == as_of else as_of,
        )

    period = period_for(company, as_of)
    entry = _new_je(
        company, "JE-FX-", "REVALUATION", as_of,
        f"Exchange revaluation at {as_of:%d %b %Y} — "
        f"{len(position['rows'])} currency(ies), "
        f"receivables {receivable:,.2f}, payables {payable:,.2f}",
    )
    entry.source_document_type = REVALUATION
    entry.source_document_id = str(period.pk) if period else ""
    entry.save(update_fields=[
        "source_document_type", "source_document_id", "updated_at"])

    accounts = _accounts(company)
    gain, loss = accounts["gain"], accounts["loss"]
    revalued_receivables, revalued_payables = accounts["ar"], accounts["ap"]

    if receivable > 0:
        _dr(entry, revalued_receivables, receivable,
            f"Receivables worth more at {as_of:%d %b %Y}")
        _cr(entry, gain, receivable, "Unrealised exchange gain")
    elif receivable < 0:
        _cr(entry, revalued_receivables, -receivable,
            f"Receivables worth less at {as_of:%d %b %Y}")
        _dr(entry, loss, -receivable, "Unrealised exchange loss")

    if payable > 0:
        _dr(entry, loss, payable, "Unrealised exchange loss")
        _cr(entry, revalued_payables, payable,
            f"Payables worth more at {as_of:%d %b %Y}")
    elif payable < 0:
        _dr(entry, revalued_payables, -payable,
            f"Payables worth less at {as_of:%d %b %Y}")
        _cr(entry, gain, -payable, "Unrealised exchange gain")

    entry.post(user=user)
    _record_shares(entry, position)
    # A document open on the day but settled since then was valued above
    # because it was owed on the day; its share comes straight back off on
    # the day it was settled.
    sync_releases(company, user=user, position=position)
    return entry, position


# ─────────────────────────── settling releases ──────────────────────────────

CENT = Decimal("0.01")


def _document_key(document):
    return f"{type(document).__name__}:{document.pk}"


def _record_shares(entry, position):
    """Write down each document's share of a revaluation as it was booked."""
    from apps.accounting.models import RevaluationShare

    RevaluationShare.objects.bulk_create([
        RevaluationShare(
            revaluation=entry, document_key=_document_key(item["document"]),
            side=item["side"], currency=item["code"], foreign=item["foreign"],
            difference=item["difference"])
        for item in position.get("documents", []) if item["difference"]
    ])


def _shares(live, position=None):
    """[(share, document)] for every document with a part in the live revaluation.

    Read from the shares written down when it was posted. A revaluation
    posted before shares were kept has none on file; for that one the
    documents are read again as at its day, which is what this module did
    before and is right unless a settlement was backdated behind it.
    """
    from types import SimpleNamespace

    from apps.accounting.models import RevaluationShare
    from apps.procurement.models import Bill
    from apps.sales.models import Invoice

    stored = list(RevaluationShare.objects.filter(revaluation=live))
    if stored:
        models = {"Invoice": Invoice, "Bill": Bill}
        out = []
        for share in stored:
            kind, _, pk = share.document_key.partition(":")
            model = models.get(kind)
            document = model.objects.filter(pk=pk).first() if model else None
            if document is not None:
                out.append((share, document))
        return out
    if position is None or position.get("as_of") != live.posting_date:
        position = positions(live.company, live.posting_date)
    return [
        (SimpleNamespace(document_key=_document_key(item["document"]), side=item["side"],
                         foreign=item["foreign"], difference=item["difference"]),
         item["document"])
        for item in position.get("documents", []) if item["difference"]
    ]


def _released_by(entry, side):
    """How much of a document's share one release entry took back off.

    Signed the same way as the share: a release of a gain on a receivable
    credits 1350, a release of a loss on it debits 1350, and the payable side
    is the mirror on 2150. Read from the entry's own lines, so nothing about a
    release has to be remembered anywhere but the ledger.
    """
    code = RECEIVABLE_ACCOUNT if side == "ar" else PAYABLE_ACCOUNT
    total = Z
    for line in entry.lines.filter(account__code=code):
        if side == "ar":
            total += line.credit_amount - line.debit_amount
        else:
            total += line.debit_amount - line.credit_amount
    return total


def _still_owed(share, document):
    """What of the share's foreign amount the document still owes now.

    A cancelled document owes nothing: cancelling it reversed its entry, so
    the receivable or payable the revaluation was measuring is gone in full.
    Otherwise it is the document's own outstanding figure, never read as more
    than it owed on the revaluation's day (an invoice owed again after a
    receipt is given back carries its whole share again, not more).
    """
    if document.status == "CANCELLED":
        return Z
    owed = Decimal(str(document.outstanding_amount or 0))
    return min(max(owed, Z), share.foreign)


def _release_date(live, document):
    """The day a release is dated: when the document was settled or cancelled,
    never before the revaluation whose share it takes back."""
    on_date = None
    if document.status == "CANCELLED":
        undone = getattr(document.journal_entry, "reversed_by", None) \
            if document.journal_entry_id else None
        on_date = undone.posting_date if undone else datetime.date.today()
    else:
        on_date = settled_on(document) or datetime.date.today()
    return max(on_date, live.posting_date)


def _post_release(company, live, document, side, amount, on_date, user=None, why="settled"):
    """Take `amount` of one document's share of the live revaluation back off."""
    from apps.accounting.gl_service import _cr, _dr, _new_je

    accounts = _accounts(company)
    entry = _new_je(
        company, "JE-FXR-", "REVALUATION", on_date,
        f"{document.reference} {why} — release its share of {live.reference}",
    )
    entry.source_document_type = RELEASE
    entry.source_document_id = _document_key(document)
    entry.save(update_fields=["source_document_type", "source_document_id", "updated_at"])
    label = f"{document.reference} {why}: unrealised exchange released"
    if side == "ar":
        # Booked as Dr 1350 / Cr gain when the paper was worth more, and the
        # other way round when it was worth less; the release is the mirror.
        if amount > 0:
            _dr(entry, accounts["gain"], amount, label)
            _cr(entry, accounts["ar"], amount, label)
        else:
            _dr(entry, accounts["ar"], -amount, label)
            _cr(entry, accounts["loss"], -amount, label)
    else:
        if amount > 0:
            _dr(entry, accounts["ap"], amount, label)
            _cr(entry, accounts["loss"], amount, label)
        else:
            _dr(entry, accounts["gain"], -amount, label)
            _cr(entry, accounts["ap"], -amount, label)
    entry.post(user=user)
    return entry


def sync_releases(company, user=None, position=None):
    """Bring the release entries into line with what has been settled since.

    For every document with a share in the live revaluation, the part of the
    share that should have come off is the part of the document that has been
    settled since: all of it once the document is paid in full or cancelled,
    and a proportional slice for a part-payment -- 400 of a 1,000 dollar
    invoice settles 40% of it, so 40% of its unrealised difference is now
    realised on the receipt and comes off 1350. What has already come off is
    read from the standing release entries. Only the gap is posted, so
    running this twice changes nothing, and a later receipt releases only its
    own slice.

    When less should be off than is (a receipt given back, an invoice owed
    again), the latest releases are reversed until no more is off than
    should be, and any small remainder is released afresh -- the share is
    carried again from the day the settlement was undone, while the months it
    was settled keep showing it settled.
    """
    live = live_revaluation(company)
    if live is None:
        return []
    standing = {}
    for release in live_releases(company).order_by("posting_date", "created_at"):
        standing.setdefault(release.source_document_id, []).append(release)
    touched = []
    for share, document in _shares(live, position):
        document.refresh_from_db()
        if not share.difference or not share.foreign:
            continue
        owed = _still_owed(share, document)
        if owed <= Z:
            target = share.difference
        else:
            target = (share.difference * (share.foreign - owed) / share.foreign).quantize(CENT)
        releases = standing.get(share.document_key, [])
        released = sum((_released_by(r, share.side) for r in releases), Z)
        # Too much is off: give back the latest releases first.
        while releases and abs(released) > abs(target):
            last = releases.pop()
            touched.append(last.reverse(user=user))
            released -= _released_by(last, share.side)
        gap = target - released
        if gap:
            why = "cancelled" if document.status == "CANCELLED" else (
                "settled" if owed <= Z else "part-settled")
            touched.append(_post_release(
                company, live, document, share.side, gap,
                _release_date(live, document), user=user, why=why))
    return touched


def on_document_saved(document):
    """Called whenever an invoice or bill is saved; see apps.AccountingConfig.

    Cheap when nothing is revalued: the live revaluation is looked up first,
    and only a foreign paper raised on or before it can have a share in it.
    """
    company = getattr(document, "company", None)
    if company is None or not getattr(document, "journal_entry_id", None):
        return
    if (document.currency or "").upper() == book_currency(company):
        return
    live = live_revaluation(company)
    if live is None:
        return
    raised = getattr(document, "invoice_date", None) or getattr(document, "bill_date", None)
    if raised and raised > live.posting_date:
        return
    sync_releases(company)
