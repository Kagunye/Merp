from django.urls import path
from . import views

app_name = "payroll"

urlpatterns = [
    path("", views.PayrollIndexView.as_view(), name="index"),
    path("runs/", views.PayrollRunListView.as_view(), name="run_list"),
    path("runs/new/", views.PayrollRunCreateView.as_view(), name="run_create"),
    path("runs/<uuid:pk>/", views.PayrollRunDetailView.as_view(), name="run_detail"),
    path("runs/<uuid:pk>/edit/", views.PayrollRunUpdateView.as_view(), name="run_update"),
    path("runs/<uuid:pk>/calculate/", views.run_calculate, name="run_calculate"),
    path("runs/<uuid:pk>/approve/", views.run_approve, name="run_approve"),
    path("runs/<uuid:pk>/post/", views.run_post, name="run_post"),
    path("runs/<uuid:pk>/cancel/", views.run_cancel, name="run_cancel"),
    path("payslips/", views.PayslipListView.as_view(), name="payslip_list"),
    path("payslips/<uuid:pk>/", views.PayslipDetailView.as_view(), name="payslip_detail"),
    path("rules/", views.StatutoryRuleListView.as_view(), name="rule_list"),
    path("rules/new/", views.StatutoryRuleCreateView.as_view(), name="rule_create"),
    path("rules/seed/", views.rules_seed, name="rules_seed"),
    path("rules/<uuid:pk>/", views.StatutoryRuleDetailView.as_view(), name="rule_detail"),
    path("rules/<uuid:pk>/edit/", views.StatutoryRuleUpdateView.as_view(), name="rule_update"),
    path("rules/<uuid:pk>/toggle/", views.rule_deactivate, name="rule_toggle"),
    path("rules/<uuid:rule_pk>/bands/new/", views.BandCreateView.as_view(), name="band_create"),
    path("bands/<int:pk>/delete/", views.band_delete, name="band_delete"),
    path("components/", views.ComponentListView.as_view(), name="component_list"),
    path("components/new/", views.ComponentCreateView.as_view(), name="component_create"),
    path("components/<uuid:pk>/edit/", views.ComponentUpdateView.as_view(),
         name="component_update"),
]
