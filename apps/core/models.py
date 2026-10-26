"""Shared abstract models and base classes."""
import uuid

from django.db import models
from django.utils import timezone


class TimeStampedModel(models.Model):
    """Abstract base with created/modified timestamps."""
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class UUIDModel(models.Model):
    """Abstract base with UUID primary key."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class BaseModel(UUIDModel, TimeStampedModel):
    """Base model with UUID PK, timestamps, and soft delete."""
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        abstract = True

    def soft_delete(self):
        self.is_active = False
        self.save(update_fields=["is_active", "updated_at"])


class CompanyAwareModel(BaseModel):
    """Abstract model that belongs to a company."""
    company = models.ForeignKey(
        "organizations.Company",
        on_delete=models.PROTECT,
        related_name="%(app_label)s_%(class)s_set",
    )

    class Meta:
        abstract = True


class NotesMixin(models.Model):
    notes = models.TextField(blank=True, default="")

    class Meta:
        abstract = True


class AddressMixin(models.Model):
    address_line1 = models.CharField(max_length=255, blank=True, default="")
    address_line2 = models.CharField(max_length=255, blank=True, default="")
    city = models.CharField(max_length=100, blank=True, default="")
    state_province = models.CharField(max_length=100, blank=True, default="")
    postal_code = models.CharField(max_length=20, blank=True, default="")
    country = models.CharField(max_length=100, blank=True, default="Kenya")

    class Meta:
        abstract = True

    @property
    def full_address(self):
        parts = filter(bool, [
            self.address_line1, self.address_line2,
            self.city, self.state_province,
            self.postal_code, self.country,
        ])
        return ", ".join(parts)


class ContactMixin(models.Model):
    phone = models.CharField(max_length=30, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    website = models.URLField(blank=True, default="")

    class Meta:
        abstract = True


class SequenceCounter(models.Model):
    """Per-company, per-prefix sequence counter for document numbers."""
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE)
    prefix = models.CharField(max_length=20)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("company", "prefix")]

    @classmethod
    def next_number(cls, company, prefix, padding=5):
        from django.db import transaction
        with transaction.atomic():
            obj, _ = cls.objects.select_for_update().get_or_create(
                company=company, prefix=prefix, defaults={"last_number": 0}
            )
            obj.last_number += 1
            obj.save(update_fields=["last_number"])
            return f"{prefix}{str(obj.last_number).zfill(padding)}"

    @classmethod
    def peek_number(cls, company, prefix, padding=5):
        """The number the next document *would* get, without taking it.

        A create form shows this as a preview. Opening a form is not issuing a
        document: if merely looking at "New purchase order" used a number, three
        people who opened the form and walked away would leave PO-00006 to
        PO-00008 missing from the sequence, and a gap in a numbered series is
        exactly what an auditor (or KRA) asks to have explained. The number is
        taken at save time, by `claim_number`.
        """
        last = cls.objects.filter(company=company, prefix=prefix).values_list(
            "last_number", flat=True).first() or 0
        return f"{prefix}{str(last + 1).zfill(padding)}"

    @classmethod
    def claim_number(cls, company, prefix, value, taken=None, padding=5):
        """Take the number a create form is saving, at the moment it is saved.

        `value` is what the form came back with -- normally the preview from
        `peek_number`, left as it was. Three cases:

        * blank: the next number is issued, as `next_number` would;
        * in this series (prefix followed by digits) and not yet issued: it is
          issued now, and the counter moves up to it, so the saved reference is
          the one the user saw;
        * in this series but already issued, and `taken(value)` says another
          record holds it (somebody else saved while this form was open): a
          fresh number is issued instead, so two documents never share one.

        Anything else is a reference the user typed for themselves (an imported
        number, a supplier's own): it is kept as typed and the counter is left
        alone. `taken` is a callable answering "does a record already use this
        reference?"; without it an issued number is assumed free.
        """
        from django.db import transaction
        value = (value or "").strip()
        rest = value[len(prefix):] if value.startswith(prefix) else None
        if value and not (rest and rest.isdigit()):
            return value
        with transaction.atomic():
            obj, _ = cls.objects.select_for_update().get_or_create(
                company=company, prefix=prefix, defaults={"last_number": 0}
            )
            if value:
                wanted = int(rest)
                if wanted > obj.last_number:
                    obj.last_number = wanted
                    obj.save(update_fields=["last_number"])
                    return value
                if not (taken and taken(value)):
                    return value
            while True:
                obj.last_number += 1
                minted = f"{prefix}{str(obj.last_number).zfill(padding)}"
                if not (taken and taken(minted)):
                    obj.save(update_fields=["last_number"])
                    return minted
