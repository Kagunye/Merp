from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import (
    CreateView, DetailView, ListView, TemplateView, UpdateView,
)

from apps.banking.models import BankAccount
from apps.core.mixins import CompanyRequiredMixin

from .forms import ExpenseCategoryForm, ExpenseClaimForm, ExpenseClaimLineForm
from .models import ExpenseCategory, ExpenseClaim, ExpenseClaimLine


class ModuleHomeView(LoginRequiredMixin, TemplateView):
    template_name = "expenses/home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Expenses"
        company = getattr(self.request, "active_company", None)
        if company:
            claims = ExpenseClaim.objects.filter(company=company)
            ctx["recent_claims"] = claims[:10]
            ctx["pending_count"] = claims.filter(status="SUBMITTED").count()
            ctx["approved_count"] = claims.filter(status="APPROVED").count()
            ctx["paid_count"] = claims.filter(status="PAID").count()
        return ctx


class ExpenseClaimListView(LoginRequiredMixin, ListView):
    model = ExpenseClaim
    template_name = "expenses/claim_list.html"
    context_object_name = "claims"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return ExpenseClaim.objects.none()
        return ExpenseClaim.objects.filter(company=company).select_related("employee")


class ExpenseClaimCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    model = ExpenseClaim
    form_class = ExpenseClaimForm
    template_name = "expenses/claim_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        if not form.instance.reference:
            from apps.core.models import SequenceCounter
            form.instance.reference = SequenceCounter.next_number(
                self.request.active_company, "EXP-"
            )
        response = super().form_valid(form)
        messages.success(self.request, "Expense claim created. Add line items.")
        return response

    def get_success_url(self):
        return reverse("expenses:claim_detail", args=[self.object.pk])


class ExpenseClaimUpdateView(LoginRequiredMixin, UpdateView):
    model = ExpenseClaim
    form_class = ExpenseClaimForm
    template_name = "expenses/claim_form.html"

    def get_queryset(self):
        return ExpenseClaim.objects.filter(company=self.request.active_company)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_success_url(self):
        return reverse("expenses:claim_detail", args=[self.object.pk])


class ExpenseClaimDetailView(LoginRequiredMixin, DetailView):
    model = ExpenseClaim
    template_name = "expenses/claim_detail.html"
    context_object_name = "claim"

    def get_queryset(self):
        return ExpenseClaim.objects.filter(company=self.request.active_company)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["line_form"] = ExpenseClaimLineForm(company=self.request.active_company)
        ctx["payment_accounts"] = BankAccount.objects.filter(
            company=self.request.active_company, is_active=True
        ).select_related("gl_account")
        return ctx


@login_required
@require_POST
def claim_add_line(request, pk):
    claim = get_object_or_404(ExpenseClaim, pk=pk, company=request.active_company)
    if claim.status != "DRAFT":
        messages.error(request, f"Claim {claim.reference} is {claim.get_status_display()}; lines are locked.")
        return redirect("expenses:claim_detail", pk=pk)
    form = ExpenseClaimLineForm(request.POST, request.FILES, company=request.active_company)
    if form.is_valid():
        line = form.save(commit=False)
        line.claim = claim
        line.save()
        claim.total_amount = sum(l.amount for l in claim.lines.all())
        claim.save(update_fields=["total_amount"])
        messages.success(request, "Line added.")
    else:
        messages.error(request, "Could not add line. Check the inputs.")
    return redirect("expenses:claim_detail", pk=pk)


@login_required
@require_POST
def claim_submit(request, pk):
    claim = get_object_or_404(ExpenseClaim, pk=pk, company=request.active_company)
    if claim.status == "DRAFT":
        if not claim.lines.exists():
            messages.error(request, "Add at least one line item before submitting.")
        else:
            claim.status = "SUBMITTED"
            claim.save(update_fields=["status"])
            messages.success(request, "Claim submitted for approval.")
    return redirect("expenses:claim_detail", pk=pk)


@login_required
@require_POST
def claim_approve(request, pk):
    claim = get_object_or_404(ExpenseClaim, pk=pk, company=request.active_company)
    if claim.status == "SUBMITTED":
        claim.status = "APPROVED"
        claim.approved_by = request.user
        claim.approved_at = timezone.now()
        claim.save(update_fields=["status", "approved_by", "approved_at"])
        messages.success(request, "Claim approved.")
    else:
        messages.error(request, f"Claim {claim.reference} is {claim.get_status_display()}, not awaiting approval.")
    return redirect("expenses:claim_detail", pk=pk)


@login_required
@require_POST
def claim_reject(request, pk):
    claim = get_object_or_404(ExpenseClaim, pk=pk, company=request.active_company)
    if claim.status == "SUBMITTED":
        claim.status = "REJECTED"
        claim.save(update_fields=["status"])
        messages.success(request, "Claim rejected.")
    else:
        messages.error(request, f"Claim {claim.reference} is {claim.get_status_display()}, not awaiting approval.")
    return redirect("expenses:claim_detail", pk=pk)


@login_required
@require_POST
def claim_pay(request, pk):
    """Pay an approved claim: DR expense accounts, CR bank/cash, then mark PAID."""
    claim = get_object_or_404(ExpenseClaim, pk=pk, company=request.active_company)
    if claim.status != "APPROVED":
        messages.error(request, f"Only approved claims can be paid ({claim.get_status_display()}).")
        return redirect("expenses:claim_detail", pk=pk)
    if claim.journal_entry_id:
        messages.info(request, "Claim is already posted to GL.")
        return redirect("expenses:claim_detail", pk=pk)

    bank_account = None
    account_id = request.POST.get("bank_account")
    if account_id:
        bank_account = get_object_or_404(
            BankAccount, pk=account_id, company=request.active_company
        )

    from apps.accounting.gl_service import post_expense_claim
    try:
        with transaction.atomic():
            post_expense_claim(claim, bank_account=bank_account, user=request.user)
    except Exception as exc:
        messages.error(request, f"GL posting failed: {exc}")
    else:
        messages.success(request, f"Claim {claim.reference} paid and posted to GL.")
    return redirect("expenses:claim_detail", pk=pk)


class ExpenseCategoryListView(LoginRequiredMixin, ListView):
    model = ExpenseCategory
    template_name = "expenses/category_list.html"
    context_object_name = "categories"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return ExpenseCategory.objects.none()
        return ExpenseCategory.objects.filter(company=company)


class ExpenseCategoryCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    model = ExpenseCategory
    form_class = ExpenseCategoryForm
    template_name = "expenses/category_form.html"
    success_url = reverse_lazy("expenses:category_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        return super().form_valid(form)
