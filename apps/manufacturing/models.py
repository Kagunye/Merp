"""Manufacturing models: BOM, Work Orders."""
from decimal import Decimal
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

    @property
    def effective_quantity(self):
        return self.quantity * (1 + self.scrap_percent / 100)


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
