"""Inventory views."""
import datetime

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db.models import Q, Sum
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, UpdateView

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
            self.object = form.save()
            messages.success(self.request, "Stock adjustment recorded.")
            return redirect(self.success_url)
        except ValidationError as e:
            messages.error(self.request, str(e))
            return self.form_invalid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Stock Adjustment"
        return ctx
