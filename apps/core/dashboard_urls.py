from django.urls import path
from . import dashboard_views as views

app_name = "dashboard"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="index"),
    path("switch-company/<uuid:company_id>/", views.switch_company, name="switch_company"),
    path("switch-branch/<uuid:branch_id>/", views.switch_branch, name="switch_branch"),
    path("widgets/kpis/", views.kpi_widgets, name="kpi_widgets"),
    path("widgets/recent-transactions/", views.recent_transactions_widget, name="recent_transactions"),
    path("widgets/sales-chart/", views.sales_chart_widget, name="sales_chart"),
    path("widgets/cashflow-chart/", views.cashflow_chart_widget, name="cashflow_chart"),
]
