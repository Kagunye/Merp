"""CRM URLs."""
from django.urls import path
from . import views

app_name = "crm"

urlpatterns = [
    path("", views.CustomerListView.as_view(), name="index"),
    path("customers/", views.CustomerListView.as_view(), name="customer_list"),
    path("customers/new/", views.CustomerCreateView.as_view(), name="customer_create"),
    path("customers/<uuid:pk>/", views.CustomerDetailView.as_view(), name="customer_detail"),
    path("customers/<uuid:pk>/edit/", views.CustomerUpdateView.as_view(), name="customer_edit"),
    path("suppliers/", views.SupplierListView.as_view(), name="supplier_list"),
    path("suppliers/new/", views.SupplierCreateView.as_view(), name="supplier_create"),
    path("suppliers/<uuid:pk>/edit/", views.SupplierUpdateView.as_view(), name="supplier_edit"),
    path("leads/", views.LeadListView.as_view(), name="lead_list"),
    path("leads/new/", views.LeadCreateView.as_view(), name="lead_create"),
]
