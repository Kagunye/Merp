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
]
