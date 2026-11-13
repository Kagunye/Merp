"""Tags (labels): numbered stickers with the company logo, a QR code and an
optional Code128 barcode, for products, fixed assets or anything else.

A TagBatch issues a run of numbers -- TAG-00001 to TAG-00500 -- once. Each
number is a Tag row, so a lost sticker is reprinted with the same code and a
spoilt one is voided; a number is never issued twice in a company (the
per-company, per-prefix counter is claimed inside the transaction that makes
the rows, and (company, code) is unique as a backstop).

The QR code carries the tag's own code and details as plain text, never an
address on this site: a sticker on a laptop should say what it is to any
phone, and must not hand strangers a link into the ERP.
"""
from django.conf import settings
from django.db import models

from apps.core.models import BaseModel

# Label stock. Roll labels print one label per page (a label printer driven
# by the browser); A4 sheets are the common Avery-style layouts.
#   key: (name, page_w, page_h, cols, rows, label_w, label_h, top, left, col_gap, row_gap)  -- mm
SIZES = {
    "50x25": ("50 x 25 mm roll", 50, 25, 1, 1, 50, 25, 0, 0, 0, 0),
    "38x25": ("38 x 25 mm roll", 38, 25, 1, 1, 38, 25, 0, 0, 0, 0),
    "100x50": ("100 x 50 mm roll", 100, 50, 1, 1, 100, 50, 0, 0, 0, 0),
    "A4_3x8": ("A4 sheet, 3 x 8 (24 labels, 63.5 x 33.9 mm)", 210, 297, 3, 8, 63.5, 33.9, 12.9, 7.2, 2.5, 0),
    "A4_2x7": ("A4 sheet, 2 x 7 (14 labels, 99.1 x 38.1 mm)", 210, 297, 2, 7, 99.1, 38.1, 15.15, 4.65, 2.5, 0),
}
SIZE_CHOICES = [(k, v[0]) for k, v in SIZES.items()]


class TagBatch(BaseModel):
    PURPOSES = [("FREE", "Free text"), ("PRODUCT", "A product"), ("ASSET", "A fixed asset")]
    QR_CONTENTS = [("DETAILS", "Code and details"), ("CODE", "Code only")]
    CODE_TYPES = [("QR", "QR code"), ("BARCODE", "Barcode (Code128)")]

    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="tag_batches")
    number = models.CharField("Batch", max_length=30, editable=False)
    title = models.CharField(max_length=120, blank=True, default="",
                             help_text="What these tags are for, e.g. \"Office laptops\" or \"Shelf labels, aisle 3\".")
    purpose = models.CharField("Tags for", max_length=10, choices=PURPOSES, default="FREE")
    product = models.ForeignKey("inventory.Product", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    asset = models.ForeignKey("fixed_assets.FixedAsset", on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="+", verbose_name="Fixed asset")
    subject_text = models.CharField("Describes", max_length=200, blank=True, default="",
                                    help_text="Free text: what every tag in the batch is (\"Property of\", a "
                                              "department, ...). Products and assets fill this in themselves.")
    prefix = models.CharField(max_length=12, default="TAG-")
    padding = models.PositiveSmallIntegerField("Digits", default=5)
    start_number = models.PositiveIntegerField(editable=False)
    quantity = models.PositiveIntegerField()
    label_size = models.CharField(max_length=10, choices=SIZE_CHOICES, default="50x25")

    show_logo = models.BooleanField("Company logo", default=True)
    show_company = models.BooleanField("Company name", default=True)
    show_code = models.BooleanField("Code as text", default=True)
    # One machine-readable code per tag: a QR code or a barcode, not both.
    code_type = models.CharField("Tag type", max_length=8, choices=CODE_TYPES, default="QR")
    show_price = models.BooleanField("Selling price (products)", default=False)
    qr_content = models.CharField("QR holds", max_length=10, choices=QR_CONTENTS, default="DETAILS")
    line1 = models.CharField("Extra line 1", max_length=60, blank=True, default="")
    line2 = models.CharField("Extra line 2", max_length=60, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name="+")

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["company", "number"], name="tagging_batch_number_unique")]

    def __str__(self):
        return f"{self.number} {self.first_code}–{self.last_code}"

    def code_for(self, n):
        return f"{self.prefix}{str(n).zfill(self.padding)}"

    @property
    def end_number(self):
        return self.start_number + self.quantity - 1

    @property
    def first_code(self):
        return self.code_for(self.start_number)

    @property
    def last_code(self):
        return self.code_for(self.end_number)

    @property
    def size(self):
        return SIZES.get(self.label_size, SIZES["50x25"])

    @property
    def show_qr(self):
        return self.code_type == "QR"

    @property
    def show_barcode(self):
        return self.code_type == "BARCODE"

    @property
    def lines(self):
        return [x for x in (self.line1, self.line2) if x]


class Tag(models.Model):
    STATUSES = [("ACTIVE", "Active"), ("VOID", "Void")]

    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="tags")
    batch = models.ForeignKey(TagBatch, on_delete=models.CASCADE, related_name="tags")
    number = models.PositiveIntegerField()
    code = models.CharField(max_length=40)
    # What the client calls this one tag ("Reception laptop", "Shelf A3").
    # It prints on the label and goes into the QR after the code.
    name = models.CharField(max_length=80, blank=True, default="")
    status = models.CharField(max_length=6, choices=STATUSES, default="ACTIVE")
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                  blank=True, related_name="+")
    void_reason = models.CharField(max_length=200, blank=True, default="")
    print_count = models.PositiveIntegerField(default=0)
    last_printed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["number"]
        constraints = [models.UniqueConstraint(fields=["company", "code"], name="tagging_code_unique")]
        indexes = [models.Index(fields=["batch", "number"])]

    def __str__(self):
        return self.code

    @property
    def is_void(self):
        return self.status == "VOID"
