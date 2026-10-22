"""Inventory URLs."""
from django.urls import path
from . import views

app_name = "inventory"

urlpatterns = [
    path("", views.InventoryIndexView.as_view(), name="index"),
    path("stock/", views.InventoryWarehouseStockView.as_view(), name="stock_levels"),
    path("products/", views.ProductListView.as_view(), name="product_list"),
    path("products/new/", views.ProductCreateView.as_view(), name="product_create"),
    path("products/<uuid:pk>/", views.ProductDetailView.as_view(), name="product_detail"),
    path("products/<uuid:pk>/edit/", views.ProductUpdateView.as_view(), name="product_edit"),
    path("movements/", views.StockMovementListView.as_view(), name="movement_list"),
    path("movements/adjust/", views.StockAdjustmentCreateView.as_view(), name="adjustment_create"),
    path("transfer/", views.StockTransferView.as_view(), name="stock_transfer"),
    path("warehouses/<uuid:pk>/", views.WarehouseDetailView.as_view(), name="warehouse_detail"),
    path("warehouses/<uuid:pk>/count/", views.InventoryCountView.as_view(), name="inventory_count"),
    path("units/", views.UnitOfMeasureListView.as_view(), name="unit_list"),
    path("units/new/", views.UnitOfMeasureCreateView.as_view(), name="unit_create"),
    path("units/<uuid:pk>/edit/", views.UnitOfMeasureUpdateView.as_view(), name="unit_edit"),
    path("units/<uuid:pk>/deactivate/", views.unit_deactivate, name="unit_deactivate"),
    path("units/<uuid:pk>/activate/", views.unit_activate, name="unit_activate"),
    path("units/<uuid:pk>/set-base/", views.unit_set_base, name="unit_set_base"),
]
