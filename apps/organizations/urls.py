from django.urls import path
from . import views

app_name = "organizations"

urlpatterns = [
    path("", views.ModuleHomeView.as_view(), name="home"),
    # Companies
    path("companies/", views.CompanyListView.as_view(), name="company_list"),
    path("companies/new/", views.CompanyCreateView.as_view(), name="company_create"),
    path("companies/<uuid:pk>/", views.CompanyDetailView.as_view(), name="company_detail"),
    path("companies/<uuid:pk>/edit/", views.CompanyUpdateView.as_view(), name="company_edit"),
    # Branches
    path("branches/", views.BranchListView.as_view(), name="branch_list"),
    path("branches/new/", views.BranchCreateView.as_view(), name="branch_create"),
    path("branches/<uuid:pk>/", views.BranchDetailView.as_view(), name="branch_detail"),
    path("branches/<uuid:pk>/edit/", views.BranchUpdateView.as_view(), name="branch_edit"),
    # Departments
    path("departments/", views.DepartmentListView.as_view(), name="department_list"),
    path("departments/new/", views.DepartmentCreateView.as_view(), name="department_create"),
    path("departments/<uuid:pk>/edit/", views.DepartmentUpdateView.as_view(), name="department_edit"),
    # Warehouses
    path("warehouses/", views.WarehouseListView.as_view(), name="warehouse_list"),
    path("warehouses/new/", views.WarehouseCreateView.as_view(), name="warehouse_create"),
    path("warehouses/<uuid:pk>/", views.WarehouseDetailView.as_view(), name="warehouse_detail"),
    path("warehouses/<uuid:pk>/edit/", views.WarehouseUpdateView.as_view(), name="warehouse_edit"),
]
