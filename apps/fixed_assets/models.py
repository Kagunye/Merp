"""Fixed Assets models: Asset Register, Depreciation, Disposal."""
from decimal import Decimal
from django.db import models
from apps.core.models import BaseModel, NotesMixin


class AssetCategory(BaseModel):
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="asset_categories")
    name = models.CharField(max_length=100)
    default_useful_life = models.IntegerField(default=5)
    default_depreciation_method = models.CharField(
        max_length=3,
        choices=[("SLM", "Straight Line"), ("WDV", "Written Down Value")],
        default="SLM",
    )
    gl_asset_account = models.ForeignKey(
        "accounting.Account", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="asset_category_asset_set",
    )
    gl_depreciation_account = models.ForeignKey(
        "accounting.Account", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="asset_category_depreciation_set",
    )
    gl_accumulated_account = models.ForeignKey(
        "accounting.Account", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="asset_category_accumulated_set",
    )

    class Meta:
        unique_together = [("company", "name")]
        ordering = ["name"]
        verbose_name_plural = "asset categories"

    def __str__(self):
        return self.name


class FixedAsset(BaseModel, NotesMixin):
    STATUS_CHOICES = [
        ("ACTIVE", "Active"),
        ("DISPOSED", "Disposed"),
        ("WRITTEN_OFF", "Written Off"),
        ("UNDER_MAINTENANCE", "Under Maintenance"),
    ]
    DEPRECIATION_METHODS = [("SLM", "Straight Line"), ("WDV", "Written Down Value")]

    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="fixed_assets")
    code = models.CharField(max_length=50, db_index=True)
    name = models.CharField(max_length=200)
    category = models.ForeignKey(AssetCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name="assets")
    serial_number = models.CharField(max_length=100, blank=True, default="")
    location = models.CharField(max_length=200, blank=True, default="")
    purchase_date = models.DateField()
    purchase_cost = models.DecimalField(max_digits=15, decimal_places=2)
    residual_value = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    useful_life_years = models.IntegerField(default=5)
    depreciation_method = models.CharField(max_length=3, choices=DEPRECIATION_METHODS, default="SLM")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="ACTIVE", db_index=True)
    supplier = models.ForeignKey("crm.Supplier", on_delete=models.SET_NULL, null=True, blank=True)
    warranty_expiry = models.DateField(null=True, blank=True)
    accumulated_depreciation = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    book_value = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    last_depreciated = models.DateField(null=True, blank=True)

    class Meta:
        unique_together = [("company", "code")]
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} — {self.name}"

    def save(self, *args, **kwargs):
        self.book_value = self.purchase_cost - self.accumulated_depreciation
        super().save(*args, **kwargs)

    @property
    def depreciable_amount(self):
        return self.purchase_cost - self.residual_value

    @property
    def annual_depreciation_slm(self):
        if self.useful_life_years <= 0:
            return Decimal("0")
        return self.depreciable_amount / self.useful_life_years

    @property
    def monthly_depreciation_slm(self):
        return self.annual_depreciation_slm / Decimal("12")


class AssetDepreciation(BaseModel):
    asset = models.ForeignKey(FixedAsset, on_delete=models.CASCADE, related_name="depreciations")
    period_date = models.DateField()
    depreciation_amount = models.DecimalField(max_digits=15, decimal_places=2)
    accumulated_depreciation = models.DecimalField(max_digits=15, decimal_places=2)
    book_value_after = models.DecimalField(max_digits=15, decimal_places=2)
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        ordering = ["-period_date"]
        unique_together = [("asset", "period_date")]

    def __str__(self):
        return f"{self.asset.code} depreciation {self.period_date}"


class AssetDisposal(BaseModel, NotesMixin):
    asset = models.OneToOneField(FixedAsset, on_delete=models.CASCADE, related_name="disposal")
    disposal_date = models.DateField()
    disposal_type = models.CharField(
        max_length=10,
        choices=[("SALE", "Sale"), ("SCRAP", "Scrap"), ("TRANSFER", "Transfer")],
    )
    proceeds = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    gain_loss = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True
    )

    def __str__(self):
        return f"Disposal of {self.asset.code} on {self.disposal_date}"
