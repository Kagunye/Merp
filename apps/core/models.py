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
