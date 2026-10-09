"""Sales URLs."""
from django.urls import path
from . import views

app_name = "sales"

urlpatterns = [
    path("", views.SalesIndexView.as_view(), name="index"),
    path("invoices/", views.InvoiceListView.as_view(), name="invoice_list"),
    path("orders/", views.SalesOrderListView.as_view(), name="order_list"),
    path("orders/new/", views.SalesOrderCreateView.as_view(), name="order_create"),
    path("orders/<uuid:pk>/", views.SalesOrderDetailView.as_view(), name="order_detail"),
    path("invoices/new/", views.InvoiceCreateView.as_view(), name="invoice_create"),
    path("invoices/<uuid:pk>/", views.InvoiceDetailView.as_view(), name="invoice_detail"),
    path("invoices/<uuid:pk>/post/", views.InvoicePostView.as_view(), name="invoice_post"),
    path("payments/", views.CustomerPaymentListView.as_view(), name="payment_list"),
    path("payments/new/", views.CustomerPaymentCreateView.as_view(), name="payment_create"),
    path("statements/<uuid:pk>/", views.CustomerStatementView.as_view(), name="customer_statement"),
    path("aging/", views.ARAgingView.as_view(), name="ar_aging"),
]
