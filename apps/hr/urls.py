from django.urls import path
from . import views

app_name = "hr"

urlpatterns = [
    path("", views.HRIndexView.as_view(), name="index"),
    path("employees/", views.EmployeeListView.as_view(), name="employee_list"),
    path("employees/new/", views.EmployeeCreateView.as_view(), name="employee_create"),
    path("employees/<uuid:pk>/", views.EmployeeDetailView.as_view(), name="employee_detail"),
    path("employees/<uuid:pk>/contract/", views.ContractCreateView.as_view(), name="contract_create"),
    path("leaves/", views.LeaveRequestListView.as_view(), name="leave_list"),
    path("leaves/new/", views.LeaveRequestCreateView.as_view(), name="leave_create"),
    path("leaves/<uuid:pk>/", views.LeaveRequestDetailView.as_view(), name="leave_detail"),
    path("leaves/<uuid:pk>/approve/", views.leave_approve, name="leave_approve"),
]
