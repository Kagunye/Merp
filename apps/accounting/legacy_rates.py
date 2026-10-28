"""Fill in the exchange rate on documents saved before rates were stored.

Used by the sales and procurement data migrations. Works on historical models,
so it touches only fields, never model methods.
"""
from decimal import Decimal

# Ways people typed the shilling before the currency list was a choice.
ALIASES = {"KSH": "KES", "KSHS": "KES", "KSH.": "KES", "SH": "KES"}
SIX = Decimal("0.000001")


def _code(value):
    code = (value or "").strip().upper()
    return ALIASES.get(code, code)


def backfill(apps, model_name, app_label, amount_field, base_field, control_type, side):
    """Give every rate-less document of `model_name` a rate where one is provable.

    Book currency (or blank): rate 1. Foreign and posted: the rate its own entry
    booked the receivable or payable at. Foreign and never posted: left blank,
    so posting it still asks for a real rate.
    """
    Model = apps.get_model(app_label, model_name)
    Line = apps.get_model("accounting", "JournalEntryLine")
    for doc in Model.objects.filter(exchange_rate__isnull=True).select_related("company"):
        book = _code(doc.company.currency) or "KES"
        code = _code(doc.currency) or book
        rate = None
        if code == book:
            rate = Decimal("1")
        elif doc.journal_entry_id:
            entry_rate = doc.journal_entry.exchange_rate if doc.journal_entry else None
            amount = abs(getattr(doc, amount_field) or Decimal("0"))
            if entry_rate:
                rate = Decimal(str(entry_rate))
            elif amount:
                booked = sum((getattr(line, side) for line in Line.objects.filter(
                    journal_entry_id=doc.journal_entry_id,
                    account__account_type=control_type)), Decimal("0"))
                if booked > 0:
                    rate = (booked / amount).quantize(SIX)
        updates = {"currency": code[:3]}
        if rate is not None:
            updates["exchange_rate"] = rate
            if not getattr(doc, base_field):
                updates[base_field] = ((getattr(doc, amount_field) or Decimal("0")) * rate
                                       ).quantize(Decimal("0.01"))
        Model.objects.filter(pk=doc.pk).update(**updates)
