"""Fixed Assets models: Asset Register, Depreciation, Disposal."""
from decimal import Decimal, ROUND_HALF_UP
from django.core.exceptions import ValidationError
from django.db import models
from apps.core.models import BaseModel, NotesMixin

CENTS = Decimal("0.01")


def money(value):
    """A figure rounded the way the ledger rounds it: half a cent goes up."""
    if value is None:
        return Decimal("0.00")
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


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

    def clean(self):
        super().clean()
        if self.default_useful_life is None or self.default_useful_life < 1:
            raise ValidationError(
                f"'{self.name}' would last {self.default_useful_life} years. A life of "
                "zero or less has no monthly charge to work out — a category is priced "
                "over the years it is expected to serve.")
        for field in ("gl_asset_account", "gl_depreciation_account",
                      "gl_accumulated_account"):
            account = getattr(self, field)
            if account and account.company_id != self.company_id:
                raise ValidationError(
                    f"The {field.split('gl_')[1].replace('_', ' ')} account "
                    f"'{account}' belongs to another book. Every account a category "
                    "names has to be one this company's ledger can post into.")


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
    paid_into = models.ForeignKey(
        "accounting.Account", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assets_acquired",
        help_text="The bank or drawer the purchase was paid out of. Blank books "
                  "against this book's own bank account.")
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assets_acquired_by",
        help_text="The entry that put this asset into the ledger.")

    class Meta:
        unique_together = [("company", "code")]
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} — {self.name}"

    def clean(self):
        super().clean()
        cost = self.purchase_cost or Decimal("0")
        residual = self.residual_value or Decimal("0")
        if cost <= 0:
            raise ValidationError(
                f"{self.code or 'This asset'} has no purchase cost. An asset that was "
                "given away has a different register: one that was bought has a price, "
                "and the ledger needs it to charge the cost account with something.")
        if residual < 0:
            raise ValidationError(
                "A residual value cannot be below zero. What the asset is expected to "
                "be worth at the end is a floor under its book value, not a debt.")
        if residual >= cost:
            raise ValidationError(
                f"{self.code or 'This asset'} is expected to be worth {residual} at the "
                f"end of a life it cost {cost} to buy, so there is nothing here to write "
                "off. Lower the residual, or leave the asset out of the register.")
        if self.useful_life_years is None or self.useful_life_years < 1:
            raise ValidationError(
                "A useful life of less than a year has no monthly charge to work out. "
                "Something bought to be consumed inside the year is an expense, not an "
                "asset.")
        for field in ("category", "supplier", "paid_into"):
            related = getattr(self, field)
            if related and related.company_id != self.company_id:
                raise ValidationError(
                    f"The chosen {field.replace('_', ' ')} belongs to another book. "
                    "A register that points at another company's records would post "
                    "into a ledger it has no business touching.")
        if self.warranty_expiry and self.warranty_expiry < self.purchase_date:
            raise ValidationError(
                "The warranty ends before the asset was bought, which cannot be right. "
                "Check the two dates.")

    def save(self, *args, **kwargs):
        self.book_value = self.purchase_cost - self.accumulated_depreciation
        super().save(*args, **kwargs)

    @property
    def depreciable_amount(self):
        return self.purchase_cost - self.residual_value

    @property
    def remaining_depreciable(self):
        """What is left to charge before the asset sits at its residual value."""
        return self.depreciable_amount - self.accumulated_depreciation

    @property
    def is_fully_depreciated(self):
        return self.remaining_depreciable <= Decimal("0")

    @property
    def annual_depreciation_slm(self):
        if self.useful_life_years <= 0:
            return Decimal("0")
        return self.depreciable_amount / self.useful_life_years

    @property
    def monthly_depreciation_slm(self):
        return self.annual_depreciation_slm / Decimal("12")

    @property
    def monthly_depreciation_wdv(self):
        """A reducing charge: this month's share of what the asset is worth now.

        Written down value is a fraction of the book value rather than of the cost,
        so the charge falls as the asset ages. The fraction is the asset's life read
        monthly — a four-year vehicle gives up a quarter of what it is worth each
        year — and the residual floor is enforced by the cap in the run, because a
        reducing balance never arrives at a residual value on its own.
        """
        if self.useful_life_years <= 0:
            return Decimal("0")
        return self.book_value / (Decimal(self.useful_life_years) * Decimal("12"))

    @property
    def monthly_charge(self):
        """The figure this month would be charged, before the residual cap."""
        if self.depreciation_method == "WDV":
            return self.monthly_depreciation_wdv
        return self.monthly_depreciation_slm


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

    @property
    def is_reversed(self):
        """Whether the month's charge has been stood down again.

        The row and its entry both stay in the book — a reversal is a second entry
        that offsets the first — so liveness is read from the reversal link.
        """
        return bool(self.journal_entry_id and self.journal_entry.reversed_by_id)


class AssetDisposal(BaseModel, NotesMixin):
    asset = models.OneToOneField(FixedAsset, on_delete=models.CASCADE, related_name="disposal")
    disposal_date = models.DateField()
    disposal_type = models.CharField(
        max_length=10,
        choices=[("SALE", "Sale"), ("SCRAP", "Scrap"), ("TRANSFER", "Transfer")],
    )
    proceeds = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    gain_loss = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    received_into = models.ForeignKey(
        "accounting.Account", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="asset_disposals",
        help_text="The bank or drawer the proceeds were paid into. Blank books against "
                  "this book's own bank account.")
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True
    )

    def __str__(self):
        return f"Disposal of {self.asset.code} on {self.disposal_date}"

    def clean(self):
        super().clean()
        if self.proceeds is not None and self.proceeds < 0:
            raise ValidationError(
                "Proceeds below zero says somebody paid to take the asset away. Book "
                "that cost as a disposal expense with zero proceeds, not as a negative "
                "receipt.")
        if self.disposal_date and self.asset.purchase_date \
                and self.disposal_date < self.asset.purchase_date:
            raise ValidationError(
                f"The asset was bought on {self.asset.purchase_date:%d %b %Y}, so it "
                f"could not have gone on {self.disposal_date:%d %b %Y}. One of the two "
                "dates is wrong, and the difference decides whether this was a gain.")
        if self.received_into_id and self.received_into.company_id != self.asset.company_id:
            raise ValidationError(
                "The account the proceeds were paid into belongs to another book.")
        if self.disposal_date and self.asset.last_depreciated \
                and self.disposal_date < self.asset.last_depreciated:
            raise ValidationError(
                f"Depreciation has been charged through "
                f"{self.asset.last_depreciated:%d %b %Y}, so this disposal clears "
                "a book value the asset had not reached on that date. Date it "
                "after the last charge, or stand the later charges down first.")

    @property
    def reversal(self):
        """The entry that stood this disposal down, if it has been stood down."""
        entry = self.journal_entry
        return entry.reversed_by if entry and entry.reversed_by_id else None

    @property
    def is_reversed(self):
        entry = self.journal_entry
        return bool(entry and entry.reversed_by_id)
