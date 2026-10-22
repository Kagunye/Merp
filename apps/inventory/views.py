"""Inventory views."""
import datetime

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db.models import Q, Sum
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, TemplateView, UpdateView

from .forms import ProductForm, StockMovementForm
from .models import Product, StockLevel, StockMovement


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


class ProductCreateView(LoginRequiredMixin, CreateView):
    model = Product
    form_class = ProductForm
    template_name = "inventory/product_form.html"
    success_url = reverse_lazy("inventory:product_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        company = self.request.active_company
        if company:
            from .models import ProductCategory, UnitOfMeasure
            form.fields["category"].queryset = ProductCategory.objects.filter(company=company, is_active=True)
            form.fields["category"].empty_label = "— No category"
            form.fields["unit_of_measure"].queryset = UnitOfMeasure.objects.filter(company=company, is_active=True)
        return form

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

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_queryset(self):
        return Product.objects.filter(company=self.request.active_company)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        company = self.request.active_company
        if company:
            from .models import ProductCategory, UnitOfMeasure
            form.fields["category"].queryset = ProductCategory.objects.filter(company=company, is_active=True)
            form.fields["unit_of_measure"].queryset = UnitOfMeasure.objects.filter(company=company, is_active=True)
        return form

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


class StockAdjustmentCreateView(LoginRequiredMixin, CreateView):
    model = StockMovement
    form_class = StockMovementForm
    template_name = "inventory/stock_adjustment_form.html"
    success_url = reverse_lazy("inventory:movement_list")

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        company = self.request.active_company
        if company:
            form.fields["product"].queryset = Product.objects.filter(company=company, is_active=True)
            from apps.organizations.models import Warehouse
            form.fields["warehouse"].queryset = Warehouse.objects.filter(company=company, is_active=True)
        form.fields["movement_date"].initial = datetime.date.today()
        return form

    def form_valid(self, form):
        form.instance.performed_by = self.request.user
        try:
            with transaction.atomic():
                self.object = form.save()
            messages.success(self.request, "Stock adjustment recorded.")
            return redirect(self.success_url)
        except ValidationError as e:
            form.add_error("quantity", " ".join(e.messages))
            return self.form_invalid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Stock Adjustment"
        return ctx


class InventoryIndexView(LoginRequiredMixin, TemplateView):
    template_name = "inventory/index.html"

    def get_context_data(self, **kwargs):
        from decimal import Decimal
        from django.db.models import F, Sum, ExpressionWrapper, DecimalField
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if not company:
            return ctx
        ctx["product_count"] = Product.objects.filter(company=company, is_active=True).count()
        try:
            ctx["inventory_value"] = StockLevel.objects.filter(
                warehouse__company=company,
            ).aggregate(
                v=Sum(ExpressionWrapper(
                    F("quantity") * F("product__cost_price"),
                    output_field=DecimalField(),
                ))
            )["v"] or Decimal(0)
        except Exception:
            ctx["inventory_value"] = Decimal(0)
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
        ctx["total_value"] = sum((sl.quantity * (sl.product.cost_price or Decimal(0)) for sl in ctx["stock_levels"]), Decimal(0))
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
        company = request.active_company
        from apps.core import guards
        back = redirect("inventory:stock_transfer")
        reference = request.POST.get("reference") or ""
        try:
            qty = guards.parse_amount(request.POST.get("quantity") or "0", "Quantity")
            movement_date = guards.not_in_future(datetime.date.fromisoformat(
                request.POST.get("movement_date") or datetime.date.today().isoformat()), "Transfer date")
        except ValidationError as e:
            messages.error(request, " ".join(e.messages))
            return back
        except ValueError:
            messages.error(request, "Enter a valid transfer date.")
            return back

        from_w = get_object_or_404(Warehouse, pk=request.POST.get("from_warehouse"), company=company)
        to_w = get_object_or_404(Warehouse, pk=request.POST.get("to_warehouse"), company=company)
        product = get_object_or_404(Product, pk=request.POST.get("product"), company=company)
        if from_w.pk == to_w.pk:
            messages.error(request, "From and To warehouses must differ.")
            return back

        try:
            with transaction.atomic():
                StockMovement.objects.create(
                    product=product, warehouse=from_w, movement_type="TRANSFER_OUT",
                    quantity=qty, unit_cost=product.cost_price or Decimal(0),
                    reference=reference or f"TRF-{to_w.code}",
                    movement_date=movement_date, performed_by=request.user,
                    notes=f"Transfer to {to_w.name}",
                )
                StockMovement.objects.create(
                    product=product, warehouse=to_w, movement_type="TRANSFER_IN",
                    quantity=qty, unit_cost=product.cost_price or Decimal(0),
                    reference=reference or f"TRF-{from_w.code}",
                    movement_date=movement_date, performed_by=request.user,
                    notes=f"Transfer from {from_w.name}",
                )
        except ValidationError as e:
            messages.error(request, " ".join(e.messages))
            return back
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
        warehouse = get_object_or_404(Warehouse, pk=kwargs["pk"], company=company)
        from apps.core import guards
        back = redirect("inventory:inventory_count", pk=warehouse.pk)
        try:
            movement_date = guards.not_in_future(datetime.date.fromisoformat(
                request.POST.get("movement_date") or datetime.date.today().isoformat()), "Count date")
        except (ValueError, ValidationError) as e:
            messages.error(request, " ".join(getattr(e, "messages", ["Enter a valid count date."])))
            return back
        plan = []
        for sl in StockLevel.objects.filter(warehouse=warehouse).select_related("product"):
            counted = (request.POST.get(f"count_{sl.pk}") or "").strip()
            if not counted:
                continue
            try:
                counted_dec = guards.parse_amount(counted, f"{sl.product.name} count", allow_zero=True)
            except ValidationError:
                messages.error(request, f"{sl.product.name}: a count must be zero or more (you entered {counted}).")
                return back
            if counted_dec != sl.quantity:
                plan.append((sl, counted_dec - sl.quantity))
        try:
            with transaction.atomic():
                for sl, variance in plan:
                    StockMovement.objects.create(
                        product=sl.product, warehouse=warehouse,
                        movement_type="ADJUSTMENT_PLUS" if variance > 0 else "ADJUSTMENT_MINUS",
                        quantity=abs(variance),
                        unit_cost=sl.product.cost_price or Decimal(0),
                        reference=f"CYCLE-COUNT-{warehouse.code}",
                        movement_date=movement_date,
                        performed_by=request.user,
                        notes="Cycle count adjustment",
                    )
        except ValidationError as e:
            messages.error(request, " ".join(e.messages))
            return back
        count = len(plan)
        messages.success(request, f"Cycle count posted: {count} adjustment(s).")
        return redirect("inventory:warehouse_detail", pk=warehouse.pk)
