from django.urls import path
from . import views

app_name = "banking"

urlpatterns = [
    path("", views.ModuleHomeView.as_view(), name="home"),
    path("accounts/", views.BankAccountListView.as_view(), name="account_list"),
    path("accounts/new/", views.BankAccountCreateView.as_view(), name="account_create"),
    path("accounts/<uuid:pk>/", views.BankAccountDetailView.as_view(), name="account_detail"),
    path("accounts/<uuid:pk>/edit/", views.BankAccountUpdateView.as_view(), name="account_edit"),
    path("transactions/", views.BankTransactionListView.as_view(), name="transaction_list"),
    path("transactions/new/", views.BankTransactionCreateView.as_view(), name="transaction_create"),
    path("transactions/<uuid:pk>/", views.BankTransactionDetailView.as_view(), name="transaction_detail"),
    path("transactions/<uuid:pk>/edit/", views.BankTransactionUpdateView.as_view(), name="transaction_edit"),
    path("transactions/<uuid:pk>/delete/", views.BankTransactionDeleteView.as_view(), name="transaction_delete"),
    path("transfers/new/", views.BankTransferView.as_view(), name="transfer_create"),

    path("reconciliations/", views.ReconciliationListView.as_view(), name="reconciliation_list"),
    path("reconciliations/new/", views.ReconciliationCreateView.as_view(), name="reconciliation_create"),
    path("reconciliations/<uuid:pk>/", views.ReconciliationDetailView.as_view(), name="reconciliation_detail"),
    path("reconciliations/<uuid:pk>/clear/", views.reconciliation_clear, name="reconciliation_clear"),
    path("reconciliations/<uuid:pk>/post/", views.reconciliation_post, name="reconciliation_post"),
    path("reconciliations/<uuid:pk>/reverse/", views.reconciliation_reverse, name="reconciliation_reverse"),
]
