"""Accounting URLs."""
from django.urls import path
from . import views

app_name = "accounting"

urlpatterns = [
    path("", views.AccountingIndexView.as_view(), name="index"),
    path("accounts/", views.AccountListView.as_view(), name="account_list"),
    path("accounts/new/", views.AccountCreateView.as_view(), name="account_create"),
    path("accounts/<uuid:pk>/edit/", views.AccountUpdateView.as_view(), name="account_edit"),
    path("accounts/<uuid:pk>/deactivate/", views.account_deactivate, name="account_deactivate"),
    path("accounts/<uuid:pk>/activate/", views.account_activate, name="account_activate"),
    path("journal-entries/", views.JournalEntryListView.as_view(), name="journal_entry_list"),
    path("journal-entries/new/", views.JournalEntryCreateView.as_view(), name="journal_entry_create"),
    path("journal-entries/<uuid:pk>/", views.JournalEntryDetailView.as_view(), name="journal_entry_detail"),
    path("journal-entries/<uuid:pk>/edit/", views.JournalEntryUpdateView.as_view(), name="journal_entry_edit"),
    path("journal-entries/<uuid:pk>/cancel/", views.journal_entry_cancel, name="journal_entry_cancel"),
    path("journal-entries/<uuid:pk>/post/", views.journal_entry_post, name="journal_entry_post"),
    path("journal-entries/<uuid:pk>/reverse/", views.journal_entry_reverse, name="journal_entry_reverse"),
    path("trial-balance/", views.TrialBalanceView.as_view(), name="trial_balance"),
    path("general-ledger/", views.GeneralLedgerIndexView.as_view(), name="general_ledger"),
    path("accounts/<uuid:pk>/ledger/", views.AccountLedgerView.as_view(), name="account_ledger"),
    path("fiscal-years/", views.FiscalYearListView.as_view(), name="fiscal_year_list"),
    path("fiscal-years/new/", views.FiscalYearCreateView.as_view(), name="fiscal_year_create"),
    path("fiscal-years/<uuid:pk>/close/", views.fiscal_year_close, name="fiscal_year_close"),
    path("fiscal-years/<uuid:pk>/reopen/", views.fiscal_year_reopen, name="fiscal_year_reopen"),
    path("periods/<uuid:pk>/close/", views.period_close, name="period_close"),
    path("periods/<uuid:pk>/reopen/", views.period_reopen, name="period_reopen"),
    path("opening-balances/", views.OpeningBalanceView.as_view(), name="opening_balances"),
    path("currencies/", views.CurrencyListView.as_view(), name="currency_list"),
    path("currencies/new/", views.CurrencyCreateView.as_view(), name="currency_create"),
    path("currencies/<str:code>/rates/", views.ExchangeRateHistoryView.as_view(),
         name="exchange_rate_history"),
    path("rates/new/", views.ExchangeRateCreateView.as_view(), name="exchange_rate_create"),
    path("rates/<int:pk>/edit/", views.ExchangeRateUpdateView.as_view(),
         name="exchange_rate_edit"),
    path("revaluation/", views.RevaluationView.as_view(), name="revaluation"),
    path("revaluation/post/", views.revaluation_post, name="revaluation_post"),
    path("revaluation/reverse/", views.revaluation_reverse, name="revaluation_reverse"),
]
