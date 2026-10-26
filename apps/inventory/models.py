"""Inventory and warehouse models."""
from decimal import Decimal
from django.db import models, transaction
from django.core.exceptions import ValidationError
from apps.core.models import BaseModel, NotesMixin


class UnitOfMeasure(BaseModel):
    """A counting unit the company buys, holds and sells in.

    Quantities are meaningless without one: 5 of cement and 5 bags of cement
    are different stocks. Units are grouped into kinds, and each kind has a
    base unit every other unit in that kind converts through, so a purchase in
    bags can be costed per piece and stock can be counted in whatever the
    warehouse actually handles.
    """
    KINDS = [
        ("COUNT", "Count / pieces"),
        ("WEIGHT", "Weight"),
        ("VOLUME", "Volume"),
        ("LENGTH", "Length"),
        ("AREA", "Area"),
        ("TIME", "Time"),
        ("OTHER", "Other"),
    ]

    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE)
    name = models.CharField(max_length=50)
    abbreviation = models.CharField(max_length=10)
    kind = models.CharField(max_length=10, choices=KINDS, default="COUNT")
    # How much of the kind's base unit one of this unit is worth.
    base_factor = models.DecimalField(
        max_digits=15, decimal_places=6, default=Decimal("1"),
        help_text="1 of this unit equals this many of the base unit.",
    )
    is_base = models.BooleanField(default=False)

    class Meta:
        unique_together = [("company", "abbreviation")]
        ordering = ["kind", "base_factor", "name"]
        verbose_name = "unit of measure"
        verbose_name_plural = "units of measure"

    def __str__(self):
        return self.abbreviation

    @property
    def kind_label(self):
        return dict(self.KINDS).get(self.kind, self.kind)

    def to_base(self, quantity):
        if quantity is None:
            return Decimal("0")
        return Decimal(str(quantity)) * self.base_factor

    def clean(self):
        super().clean()
        errors = {}
        if self.base_factor is None or self.base_factor <= 0:
            errors["base_factor"] = "A unit must be worth more than nothing of its base unit."
        if self.is_base and self.base_factor != Decimal("1"):
            errors["base_factor"] = "A base unit is worth exactly 1 of itself."
        if not errors:
            errors.update(self._unique_base())
        if errors:
            raise ValidationError(errors)

    def _unique_base(self):
        """One base per kind per company, or conversions have no anchor."""
        peers = UnitOfMeasure.objects.filter(
            company=self.company, kind=self.kind, is_base=True, is_active=True,
        )
        if not self._state.adding:
            peers = peers.exclude(pk=self.pk)
        if self.is_base and peers.exists():
            return {"is_base": f"{self.get_kind_display()} already has a base unit "
                               f"({peers.first()})."}
        if not self.is_base and not peers.exists():
            return {"is_base": f"{self.get_kind_display()} needs a base unit before "
                               "other units can convert to it."}
        return {}


def convert_quantity(quantity, from_uom, to_uom):
    """Re-express a quantity in another unit of the same kind."""
    quantity = Decimal(str(quantity or 0))
    if not from_uom or not to_uom:
        return quantity
    if from_uom.pk == to_uom.pk:
        return quantity
    if from_uom.kind != to_uom.kind:
        raise ValidationError(
            f"Cannot convert {from_uom.abbreviation} ({from_uom.kind_label}) "
            f"to {to_uom.abbreviation} ({to_uom.kind_label}) — different kinds.")
    if not to_uom.base_factor:
        raise ValidationError(f"{to_uom.abbreviation} has no conversion factor.")
    return (from_uom.to_base(quantity) / to_uom.base_factor)


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
    # A movement that answers an order is worth more than a note on paper: the
    # receipt is what accrues the supplier's cost into stock, and the bill later
    # releases that accrual. Without the link the two cannot be squared.
    purchase_order = models.ForeignKey(
        "procurement.PurchaseOrder", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="stock_movements")
    sales_order = models.ForeignKey(
        "sales.SalesOrder", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="stock_movements")
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

    # Which movements add to the shelf; everything else takes from it.
    INCOMING = (
        "RECEIPT", "TRANSFER_IN", "ADJUSTMENT_PLUS",
        "RETURN_IN", "OPENING", "PRODUCTION_IN",
    )
    # Incoming movements that, left unpriced, take the shelf's own average.
    PRICED_BY_SHELF = ("ADJUSTMENT_PLUS", "RETURN_IN")

    class Meta:
        ordering = ["-movement_date", "-created_at"]

    def __str__(self):
        return f"{self.movement_type} {self.quantity} {self.product.sku}"

    def save(self, *args, **kwargs):
        self.total_cost = self.quantity * self.unit_cost
        # Determine whether this is a new record (no pk yet means first save)
        is_new = self._state.adding
        # The row, the stock level and the GL entry are one event: if the stock
        # check fails the movement must not survive as an orphan record.
        with transaction.atomic():
            super().save(*args, **kwargs)
            self._update_stock_level()
            if is_new:
                self._post_gl()

    def _cost_it_at(self, unit_cost):
        """Write the value this movement carries onto its own row.

        save() recomputes totals and moves the stock level, so calling it from
        inside that would take the quantity on or off a second time — the two
        money columns are updated directly instead.
        """
        self.unit_cost = Decimal(str(unit_cost)).quantize(Decimal("0.0001"))
        self.total_cost = (self.quantity * self.unit_cost).quantize(Decimal("0.01"))
        StockMovement.objects.filter(pk=self.pk).update(
            unit_cost=self.unit_cost, total_cost=self.total_cost)

    def _update_stock_level(self):
        if self.quantity is None or self.quantity <= 0:
            raise ValidationError(
                "A stock movement has to move something: the quantity must be more than zero.")
        if self.unit_cost is not None and self.unit_cost < 0:
            # A negative price would take value off the book while putting
            # goods on the shelf (or the reverse); the posting refuses to book a
            # negative figure, so the shelf would move with no entry at all.
            raise ValidationError(
                f"A unit cost cannot be negative ({self.unit_cost}). Enter what one "
                f"{self.product.name} cost, or leave it blank to use the shelf's average.")
        with transaction.atomic():
            level, _ = StockLevel.objects.select_for_update().get_or_create(
                product=self.product, warehouse=self.warehouse,
                defaults={"quantity": Decimal("0"), "avg_cost": Decimal("0")},
            )
            incoming = self.movement_type in self.INCOMING
            if not incoming:
                # Goods leave the shelf at what the shelf carries them for, and
                # never at a figure somebody typed. The shelf's value is its
                # quantity times its average; taking goods off at any other price
                # would leave the inventory account and the stock valuation
                # disagreeing by the difference for ever, with nothing on either
                # side to say why.
                if self.unit_cost != level.avg_cost:
                    self._cost_it_at(level.avg_cost)
            elif (not self.unit_cost and level.avg_cost
                  and self.movement_type in self.PRICED_BY_SHELF):
                # A movement in that nobody priced is worth what the shelf already
                # holds the goods at: a surplus found on a count, or a customer's
                # return, gains the book the average the same items stand at.
                # Goods that arrive from somewhere with a value of their own — a
                # supplier, another shelf, the production line — are not given
                # this shelf's average: a free delivery is free, and inventing a
                # value for it would put stock in the book that nobody paid for.
                self._cost_it_at(level.avg_cost)
            if incoming:
                # The new average is the value on the shelf divided by what is
                # now on it — including goods that came in at no cost, which
                # dilute the average rather than being carried at the old price.
                if level.quantity + self.quantity > 0:
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
        """Book the movements that change what the company owns.

        Goods in put stock up, against wherever their value came from; goods out
        take stock down, charged to wherever the goods went. A transfer only
        moves stock between this company's own shelves, so it books nothing.

        A movement the ledger refuses is raised, not swallowed. A shelf that
        moved while its entry failed leaves the trial balance wrong and the
        stock report lying, and neither is discoverable by the person who stood
        in the warehouse. The views that take these movements catch it and say
        so on the screen.
        """
        if not self.product.track_inventory:
            return
        from apps.accounting import gl_service

        if self.movement_type in gl_service.STOCK_IN:
            gl_service.post_stock_receipt(self)
        elif self.movement_type in gl_service.STOCK_OUT:
            gl_service.post_stock_delivery(self)
