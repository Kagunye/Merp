"""Inventory and warehouse models."""
from decimal import Decimal
from django.db import models, transaction
from django.core.exceptions import ValidationError
from apps.core.models import BaseModel, NotesMixin


class UnitOfMeasure(BaseModel):
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE)
    name = models.CharField(max_length=50)
    abbreviation = models.CharField(max_length=10)
    is_base = models.BooleanField(default=False)

    class Meta:
        unique_together = [("company", "abbreviation")]

    def __str__(self):
        return self.abbreviation


class ProductCategory(BaseModel):
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    parent = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="children")
    description = models.TextField(blank=True, default="")

    class Meta:
        verbose_name_plural = "product categories"
        unique_together = [("company", "name")]

    def __str__(self):
        return self.name


class Product(BaseModel, NotesMixin):
    PRODUCT_TYPES = [
        ("GOODS", "Physical Goods"),
        ("SERVICE", "Service"),
        ("COMBO", "Combo / Bundle"),
    ]
    VALUATION_METHODS = [
        ("FIFO", "First In First Out"),
        ("WAVG", "Weighted Average"),
    ]

    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="products")
    sku = models.CharField(max_length=100, db_index=True)
    barcode = models.CharField(max_length=100, blank=True, default="", db_index=True)
    name = models.CharField(max_length=200, db_index=True)
    description = models.TextField(blank=True, default="")
    product_type = models.CharField(max_length=10, choices=PRODUCT_TYPES, default="GOODS")
    category = models.ForeignKey(ProductCategory, on_delete=models.SET_NULL, null=True, blank=True)
    unit_of_measure = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT)
    cost_price = models.DecimalField(max_digits=15, decimal_places=4, default=Decimal("0"))
    selling_price = models.DecimalField(max_digits=15, decimal_places=4, default=Decimal("0"))
    tax_rate = models.ForeignKey("accounting.TaxRate", on_delete=models.SET_NULL, null=True, blank=True)
    reorder_level = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0"))
    reorder_quantity = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0"))
    track_inventory = models.BooleanField(default=True)
    allow_negative_stock = models.BooleanField(default=False)
    valuation_method = models.CharField(max_length=4, choices=VALUATION_METHODS, default="WAVG")
    image = models.ImageField(upload_to="products/images/", null=True, blank=True)
    income_account = models.ForeignKey(
        "accounting.Account", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    cogs_account = models.ForeignKey(
        "accounting.Account", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    inventory_account = models.ForeignKey(
        "accounting.Account", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    has_expiry = models.BooleanField(default=False)
    track_serial = models.BooleanField(default=False)
    track_batch = models.BooleanField(default=False)

    class Meta:
        unique_together = [("company", "sku")]
        ordering = ["name"]

    def __str__(self):
        return f"{self.sku} — {self.name}"

    def total_stock(self, warehouse=None):
        qs = self.stock_levels.filter(warehouse__company=self.company)
        if warehouse:
            qs = qs.filter(warehouse=warehouse)
        return qs.aggregate(t=models.Sum("quantity"))["t"] or Decimal("0")


class StockLevel(models.Model):
    """Current stock quantity per product per warehouse."""
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="stock_levels")
    warehouse = models.ForeignKey("organizations.Warehouse", on_delete=models.CASCADE, related_name="stock_levels")
    quantity = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0"))
    avg_cost = models.DecimalField(max_digits=15, decimal_places=4, default=Decimal("0"))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("product", "warehouse")]

    def __str__(self):
        return f"{self.product.sku} @ {self.warehouse.name}: {self.quantity}"


class StockMovement(BaseModel, NotesMixin):
    """Records every stock in/out event."""
    MOVEMENT_TYPES = [
        ("RECEIPT", "Goods Receipt"),
        ("DELIVERY", "Delivery / Sales Issue"),
        ("TRANSFER_OUT", "Transfer Out"),
        ("TRANSFER_IN", "Transfer In"),
        ("ADJUSTMENT_PLUS", "Adjustment Increase"),
        ("ADJUSTMENT_MINUS", "Adjustment Decrease"),
        ("RETURN_IN", "Customer Return In"),
        ("RETURN_OUT", "Supplier Return Out"),
        ("OPENING", "Opening Stock"),
        ("PRODUCTION_OUT", "Production Consumption"),
        ("PRODUCTION_IN", "Production Output"),
    ]

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="movements")
    warehouse = models.ForeignKey("organizations.Warehouse", on_delete=models.PROTECT)
    movement_type = models.CharField(max_length=20, choices=MOVEMENT_TYPES)
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    unit_cost = models.DecimalField(max_digits=15, decimal_places=4, default=Decimal("0"))
    total_cost = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    reference = models.CharField(max_length=100, blank=True, default="")
    movement_date = models.DateField(db_index=True)
    batch_number = models.CharField(max_length=100, blank=True, default="")
    serial_number = models.CharField(max_length=100, blank=True, default="")
    expiry_date = models.DateField(null=True, blank=True)
    performed_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        ordering = ["-movement_date", "-created_at"]

    def __str__(self):
        return f"{self.movement_type} {self.quantity} {self.product.sku}"

    def save(self, *args, **kwargs):
        self.total_cost = self.quantity * self.unit_cost
        # Determine whether this is a new record (no pk yet means first save)
        is_new = self._state.adding
        super().save(*args, **kwargs)
        self._update_stock_level()
        if is_new:
            self._post_gl()

    def _update_stock_level(self):
        with transaction.atomic():
            level, _ = StockLevel.objects.select_for_update().get_or_create(
                product=self.product, warehouse=self.warehouse,
                defaults={"quantity": Decimal("0"), "avg_cost": Decimal("0")},
            )
            incoming = self.movement_type in (
                "RECEIPT", "TRANSFER_IN", "ADJUSTMENT_PLUS",
                "RETURN_IN", "OPENING", "PRODUCTION_IN",
            )
            if incoming:
                if level.quantity + self.quantity > 0 and self.unit_cost > 0:
                    total_value = (level.quantity * level.avg_cost) + (self.quantity * self.unit_cost)
                    level.avg_cost = total_value / (level.quantity + self.quantity)
                level.quantity += self.quantity
            else:
                if not self.product.allow_negative_stock:
                    if level.quantity < self.quantity:
                        raise ValidationError(
                            f"Insufficient stock for {self.product}: "
                            f"available {level.quantity}, requested {self.quantity}"
                        )
                level.quantity -= self.quantity
            level.save(update_fields=["quantity", "avg_cost", "updated_at"])

    def _post_gl(self):
        """
        Post a GL entry for stock movements that affect COGS or Inventory.

        DELIVERY / sales outbound  →  DR COGS  / CR Inventory
        RECEIPT / purchase inbound →  DR Inventory / CR Accounts Payable
        ADJUSTMENT_MINUS           →  DR COGS  / CR Inventory (inventory write-down)
        ADJUSTMENT_PLUS            →  DR Inventory / CR AP (use as contra)
        Other types (TRANSFER, RETURN, etc.) are skipped — they don't change
        net asset value at the company level.
        """
        if not self.product.track_inventory:
            return
        try:
            from apps.accounting.gl_service import (
                post_stock_delivery,
                post_stock_receipt,
            )
            if self.movement_type in ("DELIVERY", "ADJUSTMENT_MINUS", "PRODUCTION_OUT"):
                post_stock_delivery(self)
            elif self.movement_type in ("RECEIPT", "OPENING", "ADJUSTMENT_PLUS", "PRODUCTION_IN"):
                post_stock_receipt(self)
        except Exception:
            # GL posting failure must never block stock operations
            pass
