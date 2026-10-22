from django.urls import path
from . import views

app_name = "procurement"

urlpatterns = [
    path("", views.ProcurementIndexView.as_view(), name="index"),
    path("requisitions/", views.PurchaseRequisitionListView.as_view(), name="requisition_list"),
    path("requisitions/new/", views.PurchaseRequisitionCreateView.as_view(), name="requisition_create"),
    path("requisitions/<uuid:pk>/", views.PurchaseRequisitionDetailView.as_view(), name="requisition_detail"),
    path("orders/<uuid:pk>/grn/", views.GRNView.as_view(), name="grn"),
    path("orders/<uuid:pk>/grn/record/", views.grn_record, name="grn_record"),
    path("orders/<uuid:pk>/lines/add/", views.po_line_add, name="po_line_add"),
    path("orders/<uuid:pk>/lines/<int:line_pk>/delete/", views.po_line_delete, name="po_line_delete"),
    path("orders/<uuid:pk>/bill/", views.po_to_bill, name="po_to_bill"),
    path("bills/", views.BillListView.as_view(), name="bill_list"),
    path("bills/new/", views.BillCreateView.as_view(), name="bill_create"),
    path("bills/<uuid:pk>/", views.BillDetailView.as_view(), name="bill_detail"),
    path("bills/<uuid:pk>/edit/", views.BillUpdateView.as_view(), name="bill_edit"),
    path("bills/<uuid:pk>/post/", views.bill_post, name="bill_post"),
    path("bills/<uuid:pk>/cancel/", views.bill_cancel, name="bill_cancel"),
    path("bills/<uuid:pk>/debit-note/", views.bill_debit_note, name="bill_debit_note"),
    path("bills/<uuid:pk>/pay/", views.bill_pay, name="bill_pay"),
    path("orders/", views.PurchaseOrderListView.as_view(), name="po_list"),
    path("orders/new/", views.PurchaseOrderCreateView.as_view(), name="po_create"),
    path("orders/<uuid:pk>/", views.PurchaseOrderDetailView.as_view(), name="po_detail"),
    path("orders/<uuid:pk>/send/", views.po_send, name="po_send"),
    path("orders/<uuid:pk>/cancel/", views.po_cancel, name="po_cancel"),
    path("requisitions/<uuid:pk>/approve/", views.requisition_approve, name="requisition_approve"),
    path("requisitions/<uuid:pk>/reject/", views.requisition_reject, name="requisition_reject"),
    path("statements/<uuid:pk>/", views.SupplierStatementView.as_view(), name="supplier_statement"),
    path("aging/", views.APAgingView.as_view(), name="ap_aging"),
]
