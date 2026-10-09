from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView, DetailView, ListView, TemplateView, UpdateView,
)

from .forms import BankAccountForm, BankTransactionForm
from .models import BankAccount, BankTransaction


class ModuleHomeView(LoginRequiredMixin, TemplateView):
    template_name = "banking/home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Banking"
        company = getattr(self.request, "active_company", None)
        if company:
            ctx["accounts"] = BankAccount.objects.filter(company=company)
            ctx["recent_transactions"] = BankTransaction.objects.filter(
                bank_account__company=company
            ).select_related("bank_account")[:10]
        return ctx


class BankAccountListView(LoginRequiredMixin, ListView):
    model = BankAccount
    template_name = "banking/account_list.html"
    context_object_name = "accounts"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return BankAccount.objects.none()
        return BankAccount.objects.filter(company=company)


class BankAccountCreateView(LoginRequiredMixin, CreateView):
    model = BankAccount
    form_class = BankAccountForm
    template_name = "banking/account_form.html"
    success_url = reverse_lazy("banking:account_list")

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        messages.success(self.request, "Bank account created.")
        return super().form_valid(form)


class BankAccountUpdateView(LoginRequiredMixin, UpdateView):
    model = BankAccount
    form_class = BankAccountForm
    template_name = "banking/account_form.html"
    success_url = reverse_lazy("banking:account_list")


class BankAccountDetailView(LoginRequiredMixin, DetailView):
    model = BankAccount
    template_name = "banking/account_detail.html"
    context_object_name = "account"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["transactions"] = self.object.transactions.all()[:50]
        return ctx


class BankTransactionListView(LoginRequiredMixin, ListView):
    model = BankTransaction
    template_name = "banking/transaction_list.html"
    context_object_name = "transactions"
    paginate_by = 50

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return BankTransaction.objects.none()
        return BankTransaction.objects.filter(
            bank_account__company=company
        ).select_related("bank_account")


class BankTransactionCreateView(LoginRequiredMixin, CreateView):
    model = BankTransaction
    form_class = BankTransactionForm
    template_name = "banking/transaction_form.html"
    success_url = reverse_lazy("banking:transaction_list")

    def form_valid(self, form):
        messages.success(self.request, "Transaction recorded.")
        return super().form_valid(form)


class BankTransactionDetailView(LoginRequiredMixin, DetailView):
    model = BankTransaction
    template_name = "banking/transaction_detail.html"
    context_object_name = "txn"
