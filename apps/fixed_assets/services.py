"""Fixed assets: put an asset in the ledger, charge it down, and take it out.

Three entries carry an asset through the book. Buying it moves the cost off the
bank and onto the balance sheet; depreciation spends that cost a month at a
time against the profit and loss; disposal clears both halves of the cost — the
part still on the books and the part already written off — and prices whatever
came back. Each of them is a journal entry of its own, so a mistake is answered
by a reversal rather than by an edit, and the register's own figures are derived
from the same charges the ledger carries.
"""
import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounting.gl_service import (
    _cr, _dr, _get_account_by_type, _get_account_safe, _new_je, ensure_account,
)
from apps.accounting.models import Account

from .models import AssetDepreciation, AssetDisposal, FixedAsset, money

Z = Decimal("0")

GAIN_CODE, GAIN_NAME = "4350", "Gain on Disposal of Assets"
LOSS_CODE, LOSS_NAME = "6950", "Loss on Disposal of Assets"


# ─────────────────────────── where the money goes ───────────────────────────

def _general_ledger_account(company):
    """A book's fixed asset account when its chart was drawn before this register.

    Codes are no guarantee — one book keeps Fixed Assets at 1500, another at
    1600 — so the name is asked before an account is invented, and the search is
    limited to assets because the figure belongs on the balance sheet.
    """
    return (Account.objects
            .filter(company=company, is_active=True, account_type="ASSET",
                    name__icontains="fixed asset")
            .order_by("code").first())


def _bank_or_cash(company, *codes):
    """The account the money actually moved through.

    A code only counts when the account behind it holds money: this book keeps
    Inventory at 1200, and paying an asset purchase out of the stock figure would
    both overstate the asset and shrink the stock without a sale to explain it.
    """
    for code in codes:
        account = _get_account_safe(company, code)
        if account and account.account_type in ("BANK", "CASH"):
            return account
    return (_get_account_by_type(company, "BANK")
            or _get_account_by_type(company, "CASH"))


def asset_accounts(asset):
    """The three accounts a charge for this asset moves between.

    A category names its own accounts when the bookkeeper wants furniture
    tracked apart from vehicles. Failing that the register falls back to the
    chart's fixed asset codes, and an old book with none of them is given the
    account it needs — a silent gap in depreciation is worse than an account
    nobody expected to appear.
    """
    company = asset.company
    category = asset.category
    cost = ((category and category.gl_asset_account)
            or _get_account_safe(company, "1610", "1600")
            or _general_ledger_account(company)
            or ensure_account(company, "1610", "Fixed Assets", "ASSET", "1600"))
    depreciation = ((category and category.gl_depreciation_account)
                    or _get_account_safe(company, "6900")
                    or ensure_account(company, "6900", "Depreciation", "EXPENSE", "6000"))
    accumulated = ((category and category.gl_accumulated_account)
                   or _get_account_safe(company, "1690")
                   or ensure_account(company, "1690", "Accumulated Depreciation",
                                     "ASSET", "1600"))
    return cost, depreciation, accumulated


def _paid_out_of(asset):
    company = asset.company
    if asset.paid_into_id:
        return asset.paid_into
    return _bank_or_cash(company, "1200", "1100")


def _received_into(disposal):
    company = disposal.asset.company
    if disposal.received_into_id:
        return disposal.received_into
    return _bank_or_cash(company, "1200", "1100")


# ─────────────────────────────── acquisition ────────────────────────────────

@transaction.atomic
def post_acquisition(asset, user=None):
    """Book the purchase: the asset onto the balance sheet, the cash out of it.

    DR  asset account (category, or 1610/1600)   purchase cost
    CR  paid_into (or 1200/1100)                 purchase cost
    """
    if asset.journal_entry_id and not asset.journal_entry.reversed_by_id:
        return asset.journal_entry
    if asset.status == "DISPOSED":
        raise ValidationError(
            f"{asset.code} has been disposed of, so its purchase is already "
            "settled in the book. Stand the disposal down before touching the "
            "entry that brought it in.")

    cost = money(asset.purchase_cost)
    if cost <= Z:
        raise ValidationError(
            f"{asset.code} has no purchase cost to book. An asset carried at "
            "nothing has nothing to depreciate and nothing to dispose of.")

    company = asset.company
    cost_account, _dep, _acc = asset_accounts(asset)
    cash_account = _paid_out_of(asset)
    if not cash_account:
        raise ValidationError(
            f"There is no bank or cash account in this book to pay {asset.code} "
            "out of. Say which account the purchase came from.")

    je = _new_je(company, "JE-FA-", "ASSET", asset.purchase_date,
                 f"Acquisition of {asset.code} — {asset.name}")
    je.source_document_type = "FixedAsset"
    je.source_document_id = str(asset.pk)
    je.save(update_fields=["source_document_type", "source_document_id", "updated_at"])

    label = f"{asset.code} {asset.name}"
    _dr(je, cost_account, cost, label)
    _cr(je, cash_account, cost, label)
    je.post(user=user)

    FixedAsset.objects.filter(pk=asset.pk).update(journal_entry=je)
    asset.journal_entry = je
    return je


@transaction.atomic
def reverse_acquisition(asset, user=None, reason=""):
    """Take the purchase back out of the book.

    Only an asset the ledger has never charged can be undone this way: a month's
    wear was worked out of this cost, so standing the purchase down first would
    leave the charge resting on a figure the book no longer carries.
    """
    entry = asset.journal_entry
    if entry is None:
        raise ValidationError(
            f"{asset.code} was never posted, so there is no acquisition entry to "
            "stand down.")
    if entry.reversed_by_id:
        raise ValidationError(
            f"{asset.code}'s acquisition has already been stood down by "
            f"{entry.reversed_by.reference}.")
    live = _live_periods(asset)
    if live:
        raise ValidationError(
            f"{asset.code} has been charged depreciation through {max(live):%b %Y}. "
            "Stand those charges down before the purchase they were worked out "
            "from.")
    disposal = getattr(asset, "disposal", None)
    if disposal and not disposal.is_reversed:
        raise ValidationError(
            f"{asset.code} has been disposed of, and the disposal cleared this "
            "cost. Stand the disposal down first.")

    reversal = entry.reverse(user=user, posting_date=asset.purchase_date)
    if reason:
        reversal.notes = f"{reversal.notes} — {reason}"
        reversal.save(update_fields=["notes", "updated_at"])
    return reversal


# ────────────────────────────── depreciation ────────────────────────────────

def _live_periods(asset):
    """Period dates whose charge is still standing in the book."""
    return [d.period_date for d in asset.depreciations.select_related("journal_entry")
            if d.journal_entry_id and not d.journal_entry.reversed_by_id]


def month_start(day):
    return day.replace(day=1)


def next_month(day):
    if day.month == 12:
        return datetime.date(day.year + 1, 1, 1)
    return datetime.date(day.year, day.month + 1, 1)


def next_charge_amount(asset):
    """What the next month would be charged, capped at what is left to charge."""
    return money(min(asset.monthly_charge, asset.remaining_depreciable))


def charge_preview(asset):
    """The lines the next month's charge would move, before it is posted.

    Shown on the asset's own screen so the figure can be checked against the
    life and the residual before it goes into the ledger.
    """
    amount = next_charge_amount(asset)
    if amount <= Z:
        return []
    _cost, expense_account, accumulated_account = asset_accounts(asset)
    return [("Dr", expense_account, amount), ("Cr", accumulated_account, amount)]


def next_charge_date(asset):
    """The month this register is waiting for, or None when there is none.

    Charged from the month the asset arrived, then forward from the last month
    standing. Suggesting it is not the same as assuming it — the run always
    asks, because catching up a month that was missed is ordinary work.
    """
    if asset.status != "ACTIVE" or asset.is_fully_depreciated:
        # A disposed asset is not being used up any more, so the screen must
        # not offer a charge the ledger would refuse.
        return None
    live = _live_periods(asset)
    start = next_month(max(live)) if live else month_start(asset.purchase_date)
    return start if start <= timezone.localdate() else None


@transaction.atomic
def depreciate(asset, period_date, user=None):
    """Charge one month's wear and post it.

    DR  depreciation expense (category, or 6900)   this month's charge
    CR  accumulated depreciation (category, 1690)
    """
    if asset.status != "ACTIVE":
        raise ValidationError(
            f"{asset.code} is {asset.get_status_display().lower()}, so it is not "
            "being used up any more. Depreciation follows use: bring the asset "
            "back before charging it again.")

    today = timezone.localdate()
    if period_date > today:
        raise ValidationError(
            f"{period_date:%d %b %Y} has not come round yet. A month's wear is "
            "charged once the month has been lived through, not before.")
    if month_start(period_date) < month_start(asset.purchase_date):
        raise ValidationError(
            f"{asset.code} was bought on {asset.purchase_date:%d %b %Y}, so it "
            f"cannot have worn down in {period_date:%b %Y}. Charge from the "
            "month it joined the register.")

    live = _live_periods(asset)
    if period_date in live:
        raise ValidationError(
            f"{asset.code} has already been charged for {period_date:%b %Y}. "
            "Two entries for one month would take the same wear twice.")
    if live and period_date < max(live):
        raise ValidationError(
            f"The last month charged for {asset.code} was {max(live):%b %Y}. "
            "Depreciation runs forward through the calendar — go back to the "
            "earlier month, stand that charge down, and work through them in "
            "order.")
    if asset.is_fully_depreciated:
        raise ValidationError(
            f"{asset.code} has been written down to its residual value of "
            f"{money(asset.residual_value)}, which is where it stays. There is "
            "nothing left to charge.")

    amount = next_charge_amount(asset)
    if amount <= Z:
        raise ValidationError(
            f"{asset.code}'s charge for {period_date:%b %Y} works out at nothing. "
            "Check the life and residual value against the cost.")

    company = asset.company
    _cost, expense_account, accumulated_account = asset_accounts(asset)
    je = _new_je(company, "JE-DEP-", "DEPRECIATION", period_date,
                 f"Depreciation {asset.code} — {asset.name} for "
                 f"{period_date:%b %Y}")
    je.source_document_type = "FixedAsset"
    je.source_document_id = str(asset.pk)
    je.save(update_fields=["source_document_type", "source_document_id", "updated_at"])

    label = f"{asset.code} {asset.name}"
    _dr(je, expense_account, amount, label)
    _cr(je, accumulated_account, amount, label)
    je.post(user=user)

    new_accumulated = money(asset.accumulated_depreciation + amount)
    row, _created = AssetDepreciation.objects.get_or_create(
        asset=asset, period_date=period_date,
        defaults={
            "depreciation_amount": amount,
            "accumulated_depreciation": new_accumulated,
            "book_value_after": money(asset.purchase_cost - new_accumulated),
            "journal_entry": je,
        },
    )
    if not _created:
        # The month had been charged and stood down again; the row is the
        # register's memory of the period, so a fresh charge reuses it.
        AssetDepreciation.objects.filter(pk=row.pk).update(
            depreciation_amount=amount,
            accumulated_depreciation=new_accumulated,
            book_value_after=money(asset.purchase_cost - new_accumulated),
            journal_entry=je,
        )
        row.refresh_from_db()

    asset.accumulated_depreciation = new_accumulated
    asset.last_depreciated = period_date
    asset.save(update_fields=["accumulated_depreciation", "book_value",
                              "last_depreciated", "updated_at"])
    return row


@transaction.atomic
def depreciate_all(company, period_date, user=None):
    """Every active asset's charge for one month, in a single pass.

    An asset that cannot be charged is reported rather than allowed to abort the
    run: a register of forty vehicles should not fail because one of them was
    bought last week.
    """
    charged, skipped = [], []
    assets = (FixedAsset.objects.filter(company=company, status="ACTIVE", is_active=True)
              .select_related("category", "paid_into").order_by("code"))
    for asset in assets:
        try:
            row = depreciate(asset, period_date, user=user)
        except ValidationError as exc:
            skipped.append((asset, exc.messages[0]))
        else:
            charged.append((asset, row))
    return charged, skipped


@transaction.atomic
def reverse_depreciation(depreciation, user=None, reason=""):
    """Stand a month's charge down again, and put the register back."""
    asset = depreciation.asset
    if depreciation.is_reversed:
        raise ValidationError(
            f"{asset.code}'s charge for {depreciation.period_date:%b %Y} has "
            "already been stood down.")
    disposal = getattr(asset, "disposal", None)
    if disposal and not disposal.is_reversed:
        raise ValidationError(
            f"{asset.code} has been disposed of, and the disposal cleared the "
            "accumulated depreciation this charge fed. Stand the disposal down "
            "first.")

    reversal = depreciation.journal_entry.reverse(
        user=user, posting_date=depreciation.period_date)
    if reason:
        reversal.notes = f"{reversal.notes} — {reason}"
        reversal.save(update_fields=["notes", "updated_at"])

    live = sorted(p for p in _live_periods(asset) if p != depreciation.period_date)
    asset.accumulated_depreciation = money(asset.accumulated_depreciation
                                           - depreciation.depreciation_amount)
    asset.last_depreciated = live[-1] if live else None
    asset.save(update_fields=["accumulated_depreciation", "book_value",
                              "last_depreciated", "updated_at"])
    return reversal


# ──────────────────────────────── disposal ──────────────────────────────────

@transaction.atomic
def post_disposal(disposal, user=None):
    """Take an asset out of the book and price what came back.

    DR  accumulated depreciation            the wear already charged
    DR  received_into (or 1200/1100)        the proceeds
    DR  loss on disposal (6950)             when the book was worth more
    CR  asset account (category, 1610/1600) the original cost
    CR  gain on disposal (4350)             when the proceeds beat the book
    """
    asset = disposal.asset
    if disposal.journal_entry_id and not disposal.journal_entry.reversed_by_id:
        return disposal.journal_entry

    company = asset.company
    cost = money(asset.purchase_cost)
    accumulated = money(asset.accumulated_depreciation)
    book_value = money(cost - accumulated)
    proceeds = money(disposal.proceeds or Z)
    gain_loss = money(proceeds - book_value)

    cost_account, _exp, accumulated_account = asset_accounts(asset)
    bank_account = _received_into(disposal)
    if not bank_account:
        raise ValidationError(
            f"There is no bank or cash account in this book to receive "
            f"{asset.code}'s proceeds. Say where the money went.")

    label = f"{asset.code} {asset.name}"
    je = _new_je(company, "JE-DIS-", "DISPOSAL", disposal.disposal_date,
                 f"Disposal of {asset.code} — {asset.name} "
                 f"({disposal.get_disposal_type_display().lower()})")
    je.source_document_type = "AssetDisposal"
    je.source_document_id = str(disposal.pk)
    je.save(update_fields=["source_document_type", "source_document_id", "updated_at"])

    _dr(je, accumulated_account, accumulated, f"Depreciation charged: {label}")
    _dr(je, bank_account, proceeds, f"Proceeds: {label}")
    _cr(je, cost_account, cost, f"Cost removed: {label}")
    if gain_loss > Z:
        gain = (_get_account_safe(company, GAIN_CODE)
                or ensure_account(company, GAIN_CODE, GAIN_NAME, "REVENUE", "4300"))
        _cr(je, gain, gain_loss, f"Gain on disposal: {label}")
    elif gain_loss < Z:
        loss = (_get_account_safe(company, LOSS_CODE)
                or ensure_account(company, LOSS_CODE, LOSS_NAME, "EXPENSE", "6000"))
        _dr(je, loss, -gain_loss, f"Loss on disposal: {label}")
    je.post(user=user)

    AssetDisposal.objects.filter(pk=disposal.pk).update(
        journal_entry=je, gain_loss=gain_loss)
    disposal.journal_entry = je
    disposal.gain_loss = gain_loss
    asset.status = "DISPOSED"
    asset.save(update_fields=["status", "book_value", "updated_at"])
    return je


@transaction.atomic
def reverse_disposal(disposal, user=None, reason=""):
    """Put a disposed asset back on the register, exactly as it stood."""
    if disposal.is_reversed:
        raise ValidationError(
            f"{disposal.asset.code} has already been brought back. The disposal "
            "entry is offset by a reversal of its own.")
    reversal = disposal.journal_entry.reverse(user=user)
    if reason:
        reversal.notes = f"{reversal.notes} — {reason}"
        reversal.save(update_fields=["notes", "updated_at"])
    asset = disposal.asset
    asset.status = "ACTIVE"
    asset.save(update_fields=["status", "book_value", "updated_at"])
    disposal.refresh_from_db()
    return reversal
