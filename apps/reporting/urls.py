from django.urls import path
from . import views

app_name = "reporting"

urlpatterns = [
    path("", views.ReportingIndexView.as_view(), name="index"),
    path("trial-balance/", views.TrialBalanceView.as_view(), name="trial_balance"),
    path("profit-loss/", views.ProfitLossView.as_view(), name="profit_loss"),
    path("balance-sheet/", views.BalanceSheetView.as_view(), name="balance_sheet"),
    path("aged-debtors/", views.AgedDebtorsView.as_view(), name="aged_debtors"),
    path("aged-creditors/", views.AgedCreditorsView.as_view(), name="aged_creditors"),
    path("cash-flow/", views.CashFlowView.as_view(), name="cash_flow"),
]
