from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import (
    CreateView, DetailView, ListView, TemplateView, UpdateView,
)

from apps.core.scoping import CompanyScopedMixin, company_of, get_scoped_or_404

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


# Roles that may enter claims on someone else's behalf.
CLAIM_CLERKS = {"ADMIN", "FINANCE_MANAGER", "ACCOUNTANT"}


def _is_clerk(request):
    return getattr(request, "company_role", None) in CLAIM_CLERKS or request.user.is_superuser


def _claim_form_kwargs(view, kwargs):
    from apps.core.guards import employee_of
    company = company_of(view.request)
    kwargs.update(company=company, only_self=not _is_clerk(view.request),
                  own_employee=employee_of(view.request.user, company))
    return kwargs


def _owns(request, claim):
    return claim.employee.user_id == request.user.pk


class ExpenseClaimCreateView(LoginRequiredMixin, CreateView):
    model = ExpenseClaim
    form_class = ExpenseClaimForm
    template_name = "expenses/claim_form.html"

    def get_form_kwargs(self):
        return _claim_form_kwargs(self, super().get_form_kwargs())

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        response = super().form_valid(form)
        messages.success(self.request, "Expense claim created. Add line items.")
        return response

    def get_success_url(self):
        return reverse("expenses:claim_detail", args=[self.object.pk])


class ExpenseClaimUpdateView(CompanyScopedMixin, LoginRequiredMixin, UpdateView):
    model = ExpenseClaim
    form_class = ExpenseClaimForm
    template_name = "expenses/claim_form.html"

    def dispatch(self, request, *args, **kwargs):
        claim = self.get_object()
        if claim.status != "DRAFT":
            messages.error(request, f"{claim.reference} is {claim.get_status_display().lower()} and can't be edited.")
            return redirect("expenses:claim_detail", pk=claim.pk)
        if not (_owns(request, claim) or _is_clerk(request)):
            messages.error(request, "Only the claimant or finance can edit this claim.")
            return redirect("expenses:claim_detail", pk=claim.pk)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        return _claim_form_kwargs(self, super().get_form_kwargs())

    def get_success_url(self):
        return reverse("expenses:claim_detail", args=[self.object.pk])


class ExpenseClaimDetailView(CompanyScopedMixin, LoginRequiredMixin, DetailView):
    model = ExpenseClaim
    template_name = "expenses/claim_detail.html"
    context_object_name = "claim"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["line_form"] = ExpenseClaimLineForm(company=company_of(self.request))
        from apps.accounting.gl_service import payment_accounts
        from apps.core.access import can
        role = getattr(self.request, "company_role", None)
        ctx["can_approve"] = can(self.request.user, role, "expenses", "claim_approve", "POST")
        ctx["can_pay"] = can(self.request.user, role, "expenses", "claim_pay", "POST")
        ctx["pay_accounts"] = payment_accounts(company_of(self.request))
        ctx["today"] = timezone.now().date()
        return ctx


@login_required
@require_POST
def claim_add_line(request, pk):
    claim = get_scoped_or_404(ExpenseClaim, request, pk=pk)
    if claim.status != "DRAFT":
        messages.error(request, f"{claim.reference} is {claim.get_status_display().lower()}; lines can only be "
                                f"added to a draft claim. Start a new claim for further expenses.")
        return redirect("expenses:claim_detail", pk=pk)
    if not (_owns(request, claim) or _is_clerk(request)):
        messages.error(request, "Only the claimant or finance can add to this claim.")
        return redirect("expenses:claim_detail", pk=pk)
    form = ExpenseClaimLineForm(request.POST, request.FILES, company=company_of(request))
    if form.is_valid():
        line = form.save(commit=False)
        line.claim = claim
        line.save()
        claim.total_amount = sum(l.amount for l in claim.lines.all())
        claim.save(update_fields=["total_amount"])
        messages.success(request, "Line added.")
    else:
        for field, errs in form.errors.items():
            label = form.fields[field].label if field in form.fields else ""
            messages.error(request, f"{label + ': ' if label else ''}{' '.join(errs)}")
    return redirect("expenses:claim_detail", pk=pk)


@login_required
@require_POST
def claim_submit(request, pk):
    claim = get_scoped_or_404(ExpenseClaim, request, pk=pk)
    if claim.status != "DRAFT":
        messages.error(request, f"{claim.reference} is already {claim.get_status_display().lower()}.")
    elif not (_owns(request, claim) or _is_clerk(request)):
        messages.error(request, "Only the claimant can submit this claim.")
    elif not claim.lines.exists() or claim.total_amount <= 0:
        messages.error(request, "Add at least one expense before submitting the claim.")
    else:
        claim.status = "SUBMITTED"
        claim.save(update_fields=["status"])
        messages.success(request, "Claim submitted for approval.")
    return redirect("expenses:claim_detail", pk=pk)


def _decision_blocked(request, claim):
    if claim.status != "SUBMITTED":
        return f"{claim.reference} is {claim.get_status_display().lower()}; only submitted claims can be decided."
    if _owns(request, claim):
        return "You can't decide your own expense claim; another finance manager must."
    return None


@login_required
@require_POST
def claim_approve(request, pk):
    claim = get_scoped_or_404(ExpenseClaim, request, pk=pk)
    blocked = _decision_blocked(request, claim)
    if blocked:
        messages.error(request, blocked)
    else:
        from django.core.exceptions import ValidationError
        from django.db import transaction
        from apps.accounting.gl_service import accrue_expense_claim
        try:
            with transaction.atomic():
                accrue_expense_claim(claim, user=request.user)
                claim.status = "APPROVED"
                claim.approved_by = request.user
                claim.approved_at = timezone.now()
                claim.save(update_fields=["status", "approved_by", "approved_at"])
            messages.success(request, f"Claim approved and {claim.total_amount:,.2f} recorded as owed to "
                                      f"{claim.employee}.")
        except (ValidationError, ValueError) as e:
            messages.error(request, " ".join(getattr(e, "messages", [str(e)])))
    return redirect("expenses:claim_detail", pk=pk)


@login_required
@require_POST
def claim_pay(request, pk):
    """Reimburse an approved claim from the bank or cash account chosen."""
    import datetime
    from django.core.exceptions import ValidationError
    from apps.accounting.gl_service import pay_expense_claim, payment_accounts
    claim = get_scoped_or_404(ExpenseClaim, request, pk=pk)
    if claim.status != "APPROVED":
        messages.error(request, "Only approved claims can be paid.")
        return redirect("expenses:claim_detail", pk=pk)
    if _owns(request, claim):
        messages.error(request, "You can't pay your own expense claim; someone else in finance must.")
        return redirect("expenses:claim_detail", pk=pk)
    accounts = payment_accounts(company_of(request))
    pay_from = accounts.filter(pk=request.POST.get("pay_from")).first() or accounts.first()
    try:
        paid_on = datetime.date.fromisoformat(request.POST.get("payment_date") or timezone.now().date().isoformat())
        if pay_from is None:
            raise ValueError("Add a bank or cash account to pay from.")
        pay_expense_claim(claim, pay_from, paid_on, user=request.user)
        messages.success(request, f"Paid {claim.total_amount:,.2f} to {claim.employee} from {pay_from.name}.")
    except (ValidationError, ValueError) as e:
        messages.error(request, " ".join(getattr(e, "messages", [str(e)])))
    return redirect("expenses:claim_detail", pk=pk)


@login_required
@require_POST
def claim_reject(request, pk):
    claim = get_scoped_or_404(ExpenseClaim, request, pk=pk)
    blocked = _decision_blocked(request, claim)
    if blocked:
        messages.error(request, blocked)
    else:
        claim.status = "REJECTED"
        claim.save(update_fields=["status"])
        messages.success(request, "Claim rejected.")
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


class ExpenseCategoryCreateView(LoginRequiredMixin, CreateView):
    model = ExpenseCategory
    form_class = ExpenseCategoryForm
    template_name = "expenses/category_form.html"
    success_url = reverse_lazy("expenses:category_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = company_of(self.request)
        return kwargs

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        return super().form_valid(form)
