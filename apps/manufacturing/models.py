"""Manufacturing models: BOM, Work Orders."""
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import models
from apps.core.models import BaseModel, NotesMixin


class BillOfMaterials(BaseModel, NotesMixin):
    STATUS_CHOICES = [
        ("DRAFT", "Draft"),
        ("ACTIVE", "Active"),
        ("OBSOLETE", "Obsolete"),
    ]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="boms")
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT, related_name="boms")
    name = models.CharField(max_length=200)
    version = models.CharField(max_length=20, default="1.0")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="DRAFT", db_index=True)
    production_quantity = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("1"))

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Bill of Materials"
        verbose_name_plural = "Bills of Materials"

    def __str__(self):
        return f"{self.name} v{self.version}"


def scrap_refusal(scrap_percent):
    """Why a recipe line's scrap allowance is not a real one, or "".

    Scrap is the share of a component lost on the way into the product, so a
    line draws quantity × (1 + scrap ÷ 100). Below nothing it would draw less
    than the recipe uses — at -100% nothing at all, so the finished goods
    would carry none of that material's cost. At 100% or more, as much again
    (or more) is thrown away as goes in, which is a typing slip, not a recipe:
    250% would draw three and a half times the materials.
    """
    if scrap_percent is None:
        return ""
    if scrap_percent < 0:
        return ("Scrap cannot be negative: it is the share of the component lost on "
                "the way in. Enter 0 if none is lost.")
    if scrap_percent >= 100:
        return (f"Scrap of {scrap_percent}% would throw away as much as the recipe uses, "
                "or more. Enter a percentage from 0 up to (but not including) 100.")
    return ""


class BOMLine(models.Model):
    bom = models.ForeignKey(BillOfMaterials, on_delete=models.CASCADE, related_name="lines")
    component = models.ForeignKey("inventory.Product", on_delete=models.PROTECT, related_name="bom_usages")
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    unit_of_measure = models.ForeignKey(
        "inventory.UnitOfMeasure", on_delete=models.SET_NULL, null=True, blank=True
    )
    scrap_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.component.name} x {self.quantity}"

    def clean(self):
        super().clean()
        error = scrap_refusal(self.scrap_percent)
        if error:
            raise ValidationError({"scrap_percent": error})

    def save(self, *args, **kwargs):
        # Checked on every save as well as in the form: a recipe line written
        # by an import or a script draws materials through the same sum.
        error = scrap_refusal(self.scrap_percent)
        if error:
            raise ValidationError({"scrap_percent": error})
        super().save(*args, **kwargs)

    @property
    def effective_quantity(self):
        return self.quantity * (1 + self.scrap_percent / 100)

    def stock_quantity(self, batches=Decimal("1")):
        """What this line takes out of stock, in the unit the component is
        counted in — including scrap.

        A BOM can be written in the unit the workshop thinks in (bags of mix),
        while the store keeps that item in pieces. Consuming the raw BOM number
        would issue 2 of whatever the store calls "one", so the line converts
        through the units' factors and only then hits the ledger.
        """
        from apps.inventory.models import convert_quantity
        quantity = self.effective_quantity * batches
        if not self.unit_of_measure_id:
            return quantity
        return convert_quantity(
            quantity, self.unit_of_measure, self.component.unit_of_measure)


class WorkOrder(BaseModel, NotesMixin):
    STATUS_CHOICES = [
        ("DRAFT", "Draft"),
        ("RELEASED", "Released"),
        ("IN_PROGRESS", "In Progress"),
        ("COMPLETED", "Completed"),
        ("CANCELLED", "Cancelled"),
    ]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="work_orders")
    reference = models.CharField(max_length=50, db_index=True)
    bom = models.ForeignKey(BillOfMaterials, on_delete=models.PROTECT, related_name="work_orders")
    planned_quantity = models.DecimalField(max_digits=12, decimal_places=4)
    actual_quantity = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0"))
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default="DRAFT", db_index=True)
    planned_start = models.DateField()
    planned_end = models.DateField(null=True, blank=True)
    actual_start = models.DateField(null=True, blank=True)
    actual_end = models.DateField(null=True, blank=True)
    warehouse = models.ForeignKey("organizations.Warehouse", on_delete=models.SET_NULL, null=True, blank=True)
    cost = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.reference


class WorkOrderConsumption(models.Model):
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="consumptions")
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT)
    planned_quantity = models.DecimalField(max_digits=12, decimal_places=4)
    actual_quantity = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0"))

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.product.name} x {self.actual_quantity}"
