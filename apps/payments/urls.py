from django.urls import path

from . import views

app_name = "payments"

urlpatterns = [
    path("", views.home, name="home"),
    path("gateways/<str:provider>/", views.gateway_edit, name="gateway_edit"),
    path("gateways/<str:provider>/check/", views.gateway_check, name="gateway_check"),
    path("gateways/<str:provider>/register-c2b/", views.gateway_register_c2b, name="gateway_register_c2b"),
    path("invoices/<uuid:pk>/request/", views.invoice_request, name="invoice_request"),
    path("till/mpesa/", views.pos_mpesa_start, name="pos_mpesa_start"),
    path("requests/<uuid:pk>/status/", views.request_status, name="request_status"),
    # gateways calling back (no session; secret key in the address)
    path("callback/<str:provider>/<str:key>/", views.callback, name="callback"),
    path("pesapal/ipn/<str:key>/", views.pesapal_ipn, name="pesapal_ipn"),
    path("return/<str:key>/", views.returned, name="returned"),
    path("mpesa/c2b/<str:key>/confirm/", views.c2b_confirm, name="c2b_confirm"),
    path("mpesa/c2b/<str:key>/validate/", views.c2b_validate, name="c2b_validate"),
    path("simulate/<str:key>/", views.simulate, name="simulate"),
]
