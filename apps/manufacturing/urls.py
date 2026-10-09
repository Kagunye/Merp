from django.urls import path
from . import views

app_name = "manufacturing"

urlpatterns = [
    path("", views.WorkOrderListView.as_view(), name="index"),
    path("boms/", views.BOMListView.as_view(), name="bom_list"),
    path("boms/new/", views.BOMCreateView.as_view(), name="bom_create"),
    path("boms/<uuid:pk>/", views.BOMDetailView.as_view(), name="bom_detail"),
    path("work-orders/", views.WorkOrderListView.as_view(), name="work_order_list"),
    path("work-orders/new/", views.WorkOrderCreateView.as_view(), name="work_order_create"),
    path("work-orders/<uuid:pk>/", views.WorkOrderDetailView.as_view(), name="work_order_detail"),
    path("work-orders/<uuid:pk>/complete/", views.work_order_complete, name="work_order_complete"),
]
