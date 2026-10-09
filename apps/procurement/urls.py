from django.urls import path
from . import views

app_name = "procurement"

urlpatterns = [
    path("", views.PurchaseOrderListView.as_view(), name="index"),
    path("bills/", views.BillListView.as_view(), name="bill_list"),
    path("bills/new/", views.BillCreateView.as_view(), name="bill_create"),
    path("bills/<uuid:pk>/", views.BillDetailView.as_view(), name="bill_detail"),
    path("orders/", views.PurchaseOrderListView.as_view(), name="po_list"),
    path("orders/new/", views.PurchaseOrderCreateView.as_view(), name="po_create"),
    path("orders/<uuid:pk>/", views.PurchaseOrderDetailView.as_view(), name="po_detail"),
]
