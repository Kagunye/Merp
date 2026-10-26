"""Inventory views."""
import datetime
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, TemplateView, UpdateView

from apps.core.mixins import CompanyRequiredMixin

from .forms import ProductForm, StockMovementForm, UnitOfMeasureForm
from .models import Product, StockLevel, StockMovement, UnitOfMeasure


class ProductListView(LoginRequiredMixin, ListView):
    template_name = "inventory/product_list.html"
    context_object_name = "products"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Product.objects.none()
        qs = Product.objects.filter(company=company, is_active=True).select_related("category", "unit_of_measure")
        q = self.request.GET.get("q")
        if q:
            qs = qs.filter(Q(sku__icontains=q) | Q(name__icontains=q) | Q(barcode__icontains=q))
        ptype = self.request.GET.get("type")
        if ptype:
            qs = qs.filter(product_type=ptype)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Products"
        ctx["q"] = self.request.GET.get("q", "")
        ctx["selected_type"] = self.request.GET.get("type", "")
        ctx["product_types"] = Product.PRODUCT_TYPES
        company = self.request.active_company
        if company:
            ctx["total_products"] = Product.objects.filter(company=company, is_active=True).count()
            ctx["low_stock_count"] = self._get_low_stock_count(company)
        return ctx

    def _get_low_stock_count(self, company):
        low = 0
        for p in Product.objects.filter(company=company, is_active=True, track_inventory=True):
            if p.total_stock() <= p.reorder_level:
                low += 1
        return low


class ProductCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    model = Product
    form_class = ProductForm
    template_name = "inventory/product_form.html"
    success_url = reverse_lazy("inventory:product_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        messages.success(self.request, f"Product '{form.instance.name}' created.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "New Product"
        ctx["action"] = "Create Product"
        return ctx


class ProductUpdateView(LoginRequiredMixin, UpdateView):
    model = Product
    form_class = ProductForm
    template_name = "inventory/product_form.html"
    success_url = reverse_lazy("inventory:product_list")

    def get_queryset(self):
        return Product.objects.filter(company=self.request.active_company)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def form_valid(self, form):
        messages.success(self.request, f"Product '{form.instance.name}' updated.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit: {self.object.name}"
        ctx["action"] = "Save Changes"
        return ctx


class ProductDetailView(LoginRequiredMixin, DetailView):
    model = Product
    template_name = "inventory/product_detail.html"
    context_object_name = "product"

    def get_queryset(self):
        return Product.objects.filter(company=self.request.active_company)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = self.object.name
        ctx["stock_levels"] = self.object.stock_levels.select_related("warehouse").all()
        ctx["recent_movements"] = self.object.movements.order_by("-movement_date")[:20]
        return ctx


class StockMovementListView(LoginRequiredMixin, ListView):
    template_name = "inventory/stock_movement_list.html"
    context_object_name = "movements"
    paginate_by = 30

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return StockMovement.objects.none()
        qs = StockMovement.objects.filter(
            product__company=company
        ).select_related("product", "warehouse", "performed_by")
        q = self.request.GET.get("q")
        if q:
            qs = qs.filter(Q(product__name__icontains=q) | Q(reference__icontains=q))
        mtype = self.request.GET.get("type")
        if mtype:
            qs = qs.filter(movement_type=mtype)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Stock Movements"
        ctx["q"] = self.request.GET.get("q", "")
        ctx["selected_type"] = self.request.GET.get("type", "")
        ctx["movement_types"] = StockMovement.MOVEMENT_TYPES
        return ctx


class StockAdjustmentCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    model = StockMovement
    form_class = StockMovementForm
    template_name = "inventory/stock_adjustment_form.html"
    success_url = reverse_lazy("inventory:movement_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["movement_date"].initial = datetime.date.today()
        return form

    def form_valid(self, form):
        form.instance.performed_by = self.request.user
        try:
            self.object = form.save()
        except ValidationError as e:
            # The shelf (or the ledger behind it) refused the movement, and the
            # whole event was rolled back; say why in the shelf's own words.
            form.add_error(None, e.messages[0])
            messages.error(self.request, f"Stock adjustment not recorded: {e.messages[0]}")
            return self.form_invalid(form)
        messages.success(
            self.request,
            f"Stock adjustment recorded: {self.object.get_movement_type_display()} of "
            f"{self.object.quantity.normalize():f} × {self.object.product.name} at "
            f"{self.object.unit_cost} each.")
        return redirect(self.success_url)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Stock Adjustment"
        return ctx


class InventoryIndexView(LoginRequiredMixin, TemplateView):
    template_name = "inventory/index.html"

    def get_context_data(self, **kwargs):
        from decimal import Decimal
        from django.db.models import F
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if not company:
            return ctx
        ctx["product_count"] = Product.objects.filter(company=company, is_active=True).count()
        # Stock is worth what each shelf carries it at — its quantity times its
        # moving average — which is the figure the inventory account is kept
        # at. The price on the product card is only a guess at the next buy.
        ctx["inventory_value"] = sum(
            (sl.quantity * sl.avg_cost for sl in StockLevel.objects.filter(
                warehouse__company=company, product__track_inventory=True)),
            Decimal(0)).quantize(Decimal("0.01"))
        try:
            ctx["low_stock_count"] = StockLevel.objects.filter(
                warehouse__company=company,
                quantity__lte=F("product__reorder_level"),
            ).count()
        except Exception:
            ctx["low_stock_count"] = 0
        ctx["recent_movements"] = StockMovement.objects.filter(
            product__company=company,
        ).select_related("product").order_by("-created_at")[:8]
        return ctx


class InventoryWarehouseStockView(LoginRequiredMixin, TemplateView):
    """Stock-on-hand grouped by warehouse / product."""
    template_name = "inventory/stock_levels.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if company:
            ctx["stock_levels"] = StockLevel.objects.filter(
                warehouse__company=company,
            ).select_related("warehouse", "product").order_by("warehouse__name", "product__name")
        return ctx


# ------------------------- Warehousing deep-dive -------------------------
from decimal import Decimal
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST
from django.views.generic import FormView


class WarehouseDetailView(LoginRequiredMixin, TemplateView):
    """Deep view of one warehouse: all stock on hand with reorder flags
    plus recent movements."""
    template_name = "inventory/warehouse_detail.html"

    def get_context_data(self, **kwargs):
        from apps.organizations.models import Warehouse
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        warehouse = get_object_or_404(Warehouse, pk=kwargs["pk"], company=company)
        ctx["warehouse"] = warehouse
        ctx["stock_levels"] = StockLevel.objects.filter(warehouse=warehouse).select_related("product").order_by("product__name")
        ctx["recent_movements"] = StockMovement.objects.filter(
            warehouse=warehouse
        ).select_related("product", "performed_by").order_by("-movement_date", "-created_at")[:30]
        ctx["low_stock"] = [sl for sl in ctx["stock_levels"] if sl.product.reorder_level and sl.quantity <= sl.product.reorder_level]
        # Valued as the ledger values it: each shelf at its own moving average.
        ctx["total_value"] = sum((sl.quantity * sl.avg_cost for sl in ctx["stock_levels"]),
                                 Decimal(0)).quantize(Decimal("0.01"))
        return ctx


class StockTransferView(LoginRequiredMixin, TemplateView):
    """Transfer stock from one warehouse to another.
    Creates paired TRANSFER_OUT / TRANSFER_IN movements."""
    template_name = "inventory/stock_transfer.html"

    def get_context_data(self, **kwargs):
        from apps.organizations.models import Warehouse
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if not company:
            return ctx
        ctx["warehouses"] = Warehouse.objects.filter(company=company, is_active=True)
        ctx["products"] = Product.objects.filter(company=company, is_active=True, track_inventory=True)
        return ctx

    def post(self, request, *args, **kwargs):
        from apps.organizations.models import Warehouse
        from apps.inventory.numbers import QUANTITY, pick, typed_number
        company = request.active_company
        from_pk = request.POST.get("from_warehouse") or ""
        to_pk = request.POST.get("to_warehouse") or ""
        reference = request.POST.get("reference") or ""

        warehouses = Warehouse.objects.filter(company=company)
        from_w = pick(warehouses, from_pk)
        to_w = pick(warehouses, to_pk)
        if not from_w or not to_w:
            messages.error(
                request,
                f"Choose the warehouse the stock leaves and the one it goes to — "
                f"{'the sending' if not from_w else 'the receiving'} warehouse is "
                "missing or not one of this company's.")
            return redirect("inventory:stock_transfer")
        if from_w.pk == to_w.pk:
            messages.error(request, "From and To warehouses must differ.")
            return redirect("inventory:stock_transfer")
        product = pick(Product.objects.filter(company=company), request.POST.get("product"))
        if not product:
            messages.error(request, "Choose a product from this company to transfer.")
            return redirect("inventory:stock_transfer")
        if not product.track_inventory:
            messages.error(request, f"{product.name} is not stock-tracked, so there is no stock to move.")
            return redirect("inventory:stock_transfer")

        try:
            qty = typed_number(request.POST.get("quantity"), "Quantity", QUANTITY)
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
            return redirect("inventory:stock_transfer")
        if qty is None:
            messages.error(request, "Enter the quantity to transfer, a number greater than zero.")
            return redirect("inventory:stock_transfer")

        raw_date = request.POST.get("movement_date") or ""
        movement_date = datetime.date.today()
        if raw_date:
            try:
                movement_date = datetime.date.fromisoformat(raw_date)
            except ValueError:
                messages.error(request, f"'{raw_date}' is not a date. Use the date picker (YYYY-MM-DD).")
                return redirect("inventory:stock_transfer")

        try:
            with transaction.atomic():
                # The goods move at what they are carried for on the shelf they
                # leave — the stock model prices the outgoing half at that
                # shelf's average — and arrive at exactly that figure. Nothing
                # is bought or sold, so no entry is booked, and the value that
                # left one shelf is the value that lands on the other.
                out = StockMovement.objects.create(
                    product=product, warehouse=from_w, movement_type="TRANSFER_OUT",
                    quantity=qty,
                    reference=reference or f"TRF-{to_w.code}",
                    movement_date=movement_date, performed_by=request.user,
                    notes=f"Transfer to {to_w.name}",
                )
                StockMovement.objects.create(
                    product=product, warehouse=to_w, movement_type="TRANSFER_IN",
                    quantity=qty, unit_cost=out.unit_cost,
                    reference=reference or f"TRF-{from_w.code}",
                    movement_date=movement_date, performed_by=request.user,
                    notes=f"Transfer from {from_w.name}",
                )
        except ValidationError as exc:
            messages.error(request, f"Could not transfer {product.name}: {exc.messages[0]}")
            return redirect("inventory:stock_transfer")
        messages.success(request, f"Transferred {qty} × {product.name}: {from_w.name} → {to_w.name}.")
        return redirect("inventory:warehouse_detail", pk=to_w.pk)


class InventoryCountView(LoginRequiredMixin, TemplateView):
    """Cycle count for a warehouse: enter counted quantities per product;
    posts ADJUSTMENT_PLUS / ADJUSTMENT_MINUS movements for variances."""
    template_name = "inventory/inventory_count.html"

    def get_context_data(self, **kwargs):
        from apps.organizations.models import Warehouse
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        warehouse = get_object_or_404(Warehouse, pk=kwargs["pk"], company=company)
        ctx["warehouse"] = warehouse
        ctx["stock_levels"] = StockLevel.objects.filter(warehouse=warehouse).select_related("product").order_by("product__name")
        return ctx

    def post(self, request, *args, **kwargs):
        from apps.organizations.models import Warehouse
        company = request.active_company
        from apps.inventory.numbers import MONEY, QUANTITY, fits, typed_number
        warehouse = get_object_or_404(Warehouse, pk=kwargs["pk"], company=company)
        raw_date = request.POST.get("movement_date") or ""
        movement_date = datetime.date.today()
        if raw_date:
            try:
                movement_date = datetime.date.fromisoformat(raw_date)
            except ValueError:
                messages.error(request, f"Count not posted: '{raw_date}' is not a date (YYYY-MM-DD).")
                return redirect("inventory:inventory_count", pk=warehouse.pk)
        levels = list(StockLevel.objects.filter(warehouse=warehouse).select_related("product"))
        # Every box is read before anything is posted. A count is one event: a
        # sheet with one unreadable figure on it is handed back whole, rather
        # than half posted with the bad line quietly left out.
        counted = []
        problems = []
        for sl in levels:
            try:
                value = typed_number(request.POST.get(f"count_{sl.pk}"),
                                     f"Count for {sl.product.name}", QUANTITY,
                                     allow_zero=True)
            except ValidationError as exc:
                problems.append(exc.messages[0])
                continue
            if value is None:
                continue
            if not fits(abs(value - sl.quantity) * (sl.avg_cost or sl.product.cost_price
                                                      or Decimal(0)), MONEY):
                problems.append(
                    f"Count for {sl.product.name} ({value.normalize():f}) is so far from the "
                    f"{sl.quantity.normalize():f} on record that the difference is worth more "
                    "than the ledger can hold. Check the figure.")
                continue
            counted.append((sl, value))
        if problems:
            messages.error(request, "Count not posted. " + " ".join(problems))
            return redirect("inventory:inventory_count", pk=warehouse.pk)
        count = 0
        try:
            with transaction.atomic():
                for sl, counted_dec in counted:
                    variance = counted_dec - sl.quantity
                    if variance == 0:
                        continue
                    mtype = "ADJUSTMENT_PLUS" if variance > 0 else "ADJUSTMENT_MINUS"
                    # A surplus found or lost is valued at the shelf's own average
                    # — the goods counted are the goods already sitting there — and
                    # falls back to the product's last recorded cost only when the
                    # shelf has never been priced.
                    StockMovement.objects.create(
                        product=sl.product, warehouse=warehouse,
                        movement_type=mtype,
                        quantity=abs(variance),
                        unit_cost=sl.avg_cost or sl.product.cost_price or Decimal(0),
                        reference=f"CYCLE-COUNT-{warehouse.code}",
                        movement_date=movement_date,
                        performed_by=request.user,
                        notes="Cycle count adjustment",
                    )
                    count += 1
        except ValidationError as exc:
            messages.error(request, f"Count not posted: {exc.messages[0]}")
            return redirect("inventory:warehouse_detail", pk=warehouse.pk)
        messages.success(request, f"Cycle count posted: {count} adjustment(s).")
        return redirect("inventory:warehouse_detail", pk=warehouse.pk)


# ────────────────────────────── units of measure ─────────────────────────────

class UnitOfMeasureListView(LoginRequiredMixin, ListView):
    """The counting units this company trades in, grouped by kind."""
    template_name = "inventory/unit_list.html"
    context_object_name = "units"
    paginate_by = 50

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return UnitOfMeasure.objects.none()
        qs = UnitOfMeasure.objects.filter(company=company)
        if self.request.GET.get("state") != "all":
            qs = qs.filter(is_active=True)
        return qs.order_by("kind", "-is_base", "base_factor", "name")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        units = list(ctx["units"])
        base_by_kind = {u.kind: u for u in units if u.is_base}
        groups = []
        for value, label in UnitOfMeasure.KINDS:
            rows = [u for u in units if u.kind == value]
            if not rows:
                continue
            base = base_by_kind.get(value)
            for unit in rows:
                unit.base_unit = base
                unit.converted_from = (
                    "" if unit.is_base or not base else
                    f"1 {unit.abbreviation} = {fmt_quantity(unit.base_factor)} {base.abbreviation}"
                )
            groups.append({"kind": value, "label": label, "rows": rows,
                           "missing_base": base is None})
        ctx["groups"] = groups
        ctx["state"] = self.request.GET.get("state", "active")
        usage = {}
        for row in Product.objects.filter(company=company).values("unit_of_measure").annotate(n=Count("pk")):
            usage[row["unit_of_measure"]] = row["n"]
        for unit in units:
            unit.product_count = usage.get(unit.pk, 0)
        ctx["active_count"] = UnitOfMeasure.objects.filter(company=company, is_active=True).count()
        ctx["retired_count"] = UnitOfMeasure.objects.filter(company=company, is_active=False).count()
        ctx["used_count"] = Product.objects.filter(
            company=company, is_active=True, unit_of_measure__is_active=True).count()
        ctx["page_title"] = "Units of Measure"
        return ctx


def fmt_quantity(value):
    """12.000000 becomes 12; 0.001000 becomes 0.001."""
    text = format(Decimal(str(value or 0)), "f").rstrip("0").rstrip(".")
    return text or "0"


class UnitOfMeasureCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    model = UnitOfMeasure
    form_class = UnitOfMeasureForm
    template_name = "inventory/unit_form.html"
    success_url = reverse_lazy("inventory:unit_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        messages.success(self.request, f"Unit '{form.instance.name}' added.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "New Unit of Measure"
        ctx["action"] = "Create"
        return ctx


class UnitOfMeasureUpdateView(LoginRequiredMixin, UpdateView):
    model = UnitOfMeasure
    form_class = UnitOfMeasureForm
    template_name = "inventory/unit_form.html"
    success_url = reverse_lazy("inventory:unit_list")

    def get_queryset(self):
        return UnitOfMeasure.objects.filter(company=self.request.active_company)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def form_valid(self, form):
        messages.success(self.request, f"Unit '{form.instance.name}' updated.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit: {self.object.name}"
        ctx["action"] = "Save Changes"
        return ctx


@login_required
@require_POST
def unit_deactivate(request, pk):
    """Stop offering a unit on new products. Products already counting in it
    keep their history; they simply cannot be edited onto a retired unit."""
    unit = get_object_or_404(UnitOfMeasure, pk=pk, company=request.active_company)
    if unit.is_base:
        peers = UnitOfMeasure.objects.filter(
            company=unit.company, kind=unit.kind, is_active=True).exclude(pk=unit.pk)
        if peers.exists():
            messages.error(
                request,
                f"{unit.abbreviation} is the base unit of {unit.get_kind_display()} and "
                f"{peers.count()} other unit(s) convert through it. Promote one of them "
                "to base first, then retire it.",
            )
            return redirect("inventory:unit_list")
    unit.is_active = False
    unit.is_base = False
    unit.save(update_fields=["is_active", "is_base", "updated_at"])
    messages.success(request, f"Unit {unit.abbreviation} retired.")
    return redirect("inventory:unit_list")


@login_required
@require_POST
def unit_activate(request, pk):
    unit = get_object_or_404(UnitOfMeasure, pk=pk, company=request.active_company)
    unit.is_active = True
    unit.save(update_fields=["is_active", "updated_at"])
    messages.success(
        request,
        f"{unit.abbreviation} is available again. Mark it as the base unit of "
        f"{unit.get_kind_display()} if nothing else covers that kind.",
    )
    return redirect("inventory:unit_list")


@login_required
@require_POST
def unit_set_base(request, pk):
    """Make this unit the anchor of its kind, un-anchoring whatever was before."""
    unit = get_object_or_404(UnitOfMeasure, pk=pk, company=request.active_company)
    if not unit.is_active:
        messages.error(request, f"{unit.abbreviation} is retired — restore it before making it base.")
        return redirect("inventory:unit_list")
    with transaction.atomic():
        UnitOfMeasure.objects.filter(
            company=unit.company, kind=unit.kind, is_base=True,
        ).exclude(pk=unit.pk).update(is_base=False)
        unit.is_base = True
        unit.base_factor = Decimal("1")
        unit.save(update_fields=["is_base", "base_factor", "updated_at"])
    messages.success(request, f"{unit.abbreviation} is now the base unit of {unit.get_kind_display()}.")
    return redirect("inventory:unit_list")
