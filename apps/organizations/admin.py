from django.contrib import admin
from .models import Company, Branch, Department, Warehouse, FiscalYear, AccountingPeriod, CompanyMembership


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ["name", "currency", "is_active", "is_default"]
    list_filter = ["is_active", "is_default"]
    search_fields = ["name", "registration_number", "tax_id"]


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ["name", "company", "is_headquarters", "is_active"]
    list_filter = ["company", "is_active"]


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ["name", "company", "branch", "is_active"]
    list_filter = ["company", "is_active"]


@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    list_display = ["name", "company", "code", "is_active"]
    list_filter = ["company", "is_active"]


@admin.register(FiscalYear)
class FiscalYearAdmin(admin.ModelAdmin):
    list_display = ["name", "company", "start_date", "end_date", "is_current", "is_closed"]


@admin.register(CompanyMembership)
class CompanyMembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "company", "role", "is_active"]
    list_filter = ["company", "role"]
