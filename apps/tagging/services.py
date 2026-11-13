"""Issuing, printing and voiding tags."""
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.core.models import SequenceCounter

from .models import Tag

MAX_BATCH = 5000          # numbers in one batch
MAX_PRINT = 1000          # labels on one print page


def counter_key(prefix):
    """SequenceCounter.prefix for a tag prefix, kept apart from document numbers."""
    return f"TAG:{prefix}"[:20]


def next_free(company, prefix):
    last = SequenceCounter.objects.filter(company=company, prefix=counter_key(prefix)).values_list(
        "last_number", flat=True).first() or 0
    return last + 1


def subject_for(batch):
    if batch.purpose == "PRODUCT" and batch.product_id:
        p = batch.product
        return f"{p.sku} {p.name}".strip()
    if batch.purpose == "ASSET" and batch.asset_id:
        a = batch.asset
        return f"{a.code} {a.name}".strip()
    return batch.subject_text


def issue_batch(batch, start=None, user=None, names=None):
    """Save `batch` (unsaved, company set) and issue its numbers.

    The counter row is locked and moved inside the same transaction that makes
    the tags, so two people issuing at once get different numbers. A start
    number at or below one already issued is refused, never reused.
    """
    company = batch.company
    names = list(names or [])
    if not 1 <= batch.quantity <= MAX_BATCH:
        raise ValidationError(f"Issue between 1 and {MAX_BATCH} tags at a time.")
    try:
        with transaction.atomic():
            counter, _ = SequenceCounter.objects.select_for_update().get_or_create(
                company=company, prefix=counter_key(batch.prefix), defaults={"last_number": 0})
            first = start or counter.last_number + 1
            if first <= counter.last_number:
                raise ValidationError(
                    f"{batch.code_for(counter.last_number)} has been issued already. "
                    f"Start at {counter.last_number + 1} or later.")
            last = first + batch.quantity - 1
            if last > SequenceCounter.MAX_COUNTER:
                raise ValidationError("That number is too large.")
            batch.start_number = first
            batch.number = SequenceCounter.next_number(company, "TB-")
            batch.subject_text = subject_for(batch)[:200]
            batch.created_by = user if getattr(user, "is_authenticated", False) else None
            batch.save()
            Tag.objects.bulk_create([
                Tag(company=company, batch=batch, number=n, code=batch.code_for(n),
                    name=(names[i] if i < len(names) else "")[:80])
                for i, n in enumerate(range(first, last + 1))], batch_size=500)
            counter.last_number = last
            counter.save(update_fields=["last_number"])
    except IntegrityError:
        raise ValidationError("Some of those codes exist already (another prefix makes the same codes). "
                              "Choose another prefix or start number.")
    return batch


def tags_in_range(batch, first=None, last=None, include_void=False):
    qs = batch.tags.all()
    if first:
        qs = qs.filter(number__gte=first)
    if last:
        qs = qs.filter(number__lte=last)
    if not include_void:
        qs = qs.filter(status="ACTIVE")
    return qs.order_by("number")


def mark_printed(tag_ids):
    Tag.objects.filter(pk__in=list(tag_ids)).update(print_count=F("print_count") + 1,
                                                     last_printed_at=timezone.now())


def name_tags(batch, first, names):
    """Name the tags numbered first, first+1, ... from `names` (one each).
    A blank name leaves that tag as it is; returns how many were named."""
    by_number = {t.number: t for t in batch.tags.filter(number__gte=first, number__lt=first + len(names))}
    changed = []
    for i, name in enumerate(names):
        tag = by_number.get(first + i)
        if tag and name and tag.name != name:
            tag.name = name[:80]
            changed.append(tag)
    Tag.objects.bulk_update(changed, ["name"])
    return len(changed)


def void(batch, first, last, user, reason=""):
    """Void the active tags numbered first..last; returns how many."""
    if first > last:
        first, last = last, first
    return batch.tags.filter(number__gte=first, number__lte=last, status="ACTIVE").update(
        status="VOID", voided_at=timezone.now(), voided_by=user, void_reason=reason[:200])
