"""Inventory URLs."""
from django.urls import path
from . import views

app_name = "inventory"

urlpatterns = [
    path("", views.ProductListView.as_view(), name="index"),
    path("products/", views.ProductListView.as_view(), name="product_list"),
    path("products/new/", views.ProductCreateView.as_view(), name="product_create"),
    path("products/<uuid:pk>/", views.ProductDetailView.as_view(), name="product_detail"),
    path("products/<uuid:pk>/edit/", views.ProductUpdateView.as_view(), name="product_edit"),
    path("movements/", views.StockMovementListView.as_view(), name="movement_list"),
    path("movements/adjust/", views.StockAdjustmentCreateView.as_view(), name="adjustment_create"),
]
