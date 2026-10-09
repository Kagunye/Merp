from django.urls import path
from . import views

app_name = "payroll"

urlpatterns = [
    path("", views.PayrollRunListView.as_view(), name="index"),
    path("runs/", views.PayrollRunListView.as_view(), name="run_list"),
    path("runs/new/", views.PayrollRunCreateView.as_view(), name="run_create"),
    path("runs/<uuid:pk>/", views.PayrollRunDetailView.as_view(), name="run_detail"),
    path("payslips/<uuid:pk>/", views.PayslipDetailView.as_view(), name="payslip_detail"),
]
