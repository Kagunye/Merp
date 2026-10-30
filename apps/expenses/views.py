import datetime
import uuid

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
from apps.hr.access import manages_claims, own_employee

from .forms import ExpenseCategoryForm, ExpenseClaimForm, ExpenseClaimLineForm
from .models import ExpenseCategory, ExpenseClaim, ExpenseClaimLine


# A claim can be changed while it is still the claimant's own paper. Once it is
# submitted somebody else is looking at it; once approved or paid, the figures,
# the person and the date are what was agreed and what left the bank.
EDITABLE_STATUSES = ("DRAFT",)


def visible_claims(request):
    """The claims this login may see and act on in the active company.

    Finance and HR see every claim, because paying them is their work. Anybody
    else sees the claims raised in their own name and nobody else's — and a
    login with no employee record has no claims of its own at all.
    """
    company = getattr(request, "active_company", None)
    if not company:
        return ExpenseClaim.objects.none()
    claims = ExpenseClaim.objects.filter(company=company)
    if manages_claims(request.user, company):
        return claims
    mine = own_employee(request.user, company)
    return claims.filter(employee=mine) if mine else ExpenseClaim.objects.none()


def is_own_claim(request, claim):
    """Is the person acting the person being paid?"""
    return claim.employee.user_id is not None and claim.employee.user_id == request.user.pk


def refuse_own_claim(request, claim, verb):
    """Nobody approves or pays money back to themselves — the same rule leave
    and timesheets keep. A second person has to agree the money is owed."""
    messages.error(
        request,
        f"{claim.reference} is your own claim, so you cannot {verb} it. Somebody else "
        "in finance has to.")
    return redirect("expenses:claim_detail", pk=claim.pk)


def segregation_refusal(request, claim, step):
    """Why this login may not take this step on this claim, or "".

    Raising, approving and paying a claim are three different people's work.
    Being the person the money is for is one way to be too close to it
    (refuse_own_claim); having typed the claim in, or having approved it, is
    another. Without this, a finance clerk could raise a claim in a
    colleague's name, approve it and pay it out — three steps, one person, and
    the colleague never knowing money left the bank under their name.
    """
    user = request.user
    if step == "approve" and claim.created_by_id == user.pk:
        return (f"You raised {claim.reference}, so you cannot also approve it. Somebody "
                "other than the person who typed a claim in has to agree the money is "
                "owed.")
    if step == "pay":
        if claim.created_by_id == user.pk:
            return (f"You raised {claim.reference}, so you cannot also pay it. The "
                    "claim has to be paid by somebody who neither raised nor approved it.")
        if claim.approved_by_id == user.pk:
            return (f"You approved {claim.reference}, so you cannot also pay it. The "
                    "claim has to be paid by somebody who neither raised nor approved it.")
    return ""


class ModuleHomeView(LoginRequiredMixin, TemplateView):
    template_name = "expenses/home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Expenses"
        company = getattr(self.request, "active_company", None)
        if company:
            claims = visible_claims(self.request)
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
        return visible_claims(self.request).select_related("employee")


class ExpenseClaimCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    model = ExpenseClaim
    form_class = ExpenseClaimForm
    template_name = "expenses/claim_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        # Who typed it in, so they cannot also approve or pay it.
        form.instance.created_by = self.request.user
        from apps.core.models import SequenceCounter
        company = self.request.active_company
        # The preview on the form is only taken now, so two people saving
        # the same previewed number get two different claims.
        form.instance.reference = SequenceCounter.claim_number(
            company, "EXP-", form.instance.reference,
            taken=lambda ref: ExpenseClaim.objects.filter(
                company=company, reference__iexact=ref).exists())
        response = super().form_valid(form)
        messages.success(self.request, "Expense claim created. Add line items.")
        return response

    def get_success_url(self):
        return reverse("expenses:claim_detail", args=[self.object.pk])


class ExpenseClaimUpdateView(LoginRequiredMixin, UpdateView):
    """Correct a claim that is still a draft.

    A submitted claim is in somebody else's hands, and an approved or paid one
    is a decision and a payment: changing the claimant, the date or the
    reference afterwards would make the approval and the bank entry describe a
    claim that no longer exists.
    """
    model = ExpenseClaim
    form_class = ExpenseClaimForm
    template_name = "expenses/claim_form.html"

    def get_queryset(self):
        return visible_claims(self.request)

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)
        claim = self.get_object()
        if claim.status not in EDITABLE_STATUSES:
            messages.error(
                request,
                f"{claim.reference} is {claim.get_status_display().lower()}, so it can no "
                "longer be changed — only a draft claim can be edited. If it is wrong, "
                "have it rejected and raise a new claim.")
            return redirect("expenses:claim_detail", pk=claim.pk)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        kwargs["user"] = self.request.user
        return kwargs

    def get_success_url(self):
        return reverse("expenses:claim_detail", args=[self.object.pk])


class ExpenseClaimDetailView(LoginRequiredMixin, DetailView):
    model = ExpenseClaim
    template_name = "expenses/claim_detail.html"
    context_object_name = "claim"

    def get_queryset(self):
        return visible_claims(self.request)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["line_form"] = ExpenseClaimLineForm(company=self.request.active_company)
        ctx["is_own_claim"] = is_own_claim(self.request, self.object)
        # The buttons are hidden from whoever the segregation rule turns away,
        # so nobody is offered a step they will only be refused.
        ctx["cannot_approve"] = segregation_refusal(self.request, self.object, "approve")
        ctx["cannot_pay"] = segregation_refusal(self.request, self.object, "pay")
        ctx["today"] = timezone.localdate()
        ctx["payment_accounts"] = BankAccount.objects.filter(
            company=self.request.active_company, is_active=True
        ).select_related("gl_account")
        return ctx


@login_required
@require_POST
def claim_add_line(request, pk):
    claim = get_object_or_404(visible_claims(request), pk=pk)
    if claim.status not in EDITABLE_STATUSES:
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
    claim = get_object_or_404(visible_claims(request), pk=pk)
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
    claim = get_object_or_404(visible_claims(request), pk=pk)
    if is_own_claim(request, claim):
        return refuse_own_claim(request, claim, "approve")
    if not manages_claims(request.user, request.active_company):
        messages.error(request, "Only finance or HR can approve an expense claim.")
        return redirect("expenses:claim_detail", pk=pk)
    refusal = segregation_refusal(request, claim, "approve")
    if refusal:
        messages.error(request, refusal)
        return redirect("expenses:claim_detail", pk=pk)
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
    claim = get_object_or_404(visible_claims(request), pk=pk)
    if is_own_claim(request, claim):
        return refuse_own_claim(request, claim, "reject")
    if not manages_claims(request.user, request.active_company):
        messages.error(request, "Only finance or HR can reject an expense claim.")
        return redirect("expenses:claim_detail", pk=pk)
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
    """Pay an approved claim: DR expense accounts, CR bank/cash, then mark PAID.

    The entry is dated the day the money leaves — the date posted with the form,
    or today — not the day the claim was written.
    """
    claim = get_object_or_404(visible_claims(request), pk=pk)
    if is_own_claim(request, claim):
        return refuse_own_claim(request, claim, "pay")
    if not manages_claims(request.user, request.active_company):
        messages.error(request, "Only finance can pay an expense claim.")
        return redirect("expenses:claim_detail", pk=pk)
    if claim.status != "APPROVED":
        messages.error(request, f"Only approved claims can be paid ({claim.get_status_display()}).")
        return redirect("expenses:claim_detail", pk=pk)
    if claim.journal_entry_id:
        messages.info(request, "Claim is already posted to GL.")
        return redirect("expenses:claim_detail", pk=pk)
    refusal = segregation_refusal(request, claim, "pay")
    if refusal:
        messages.error(request, refusal)
        return redirect("expenses:claim_detail", pk=pk)

    bank_account = None
    account_id = (request.POST.get("bank_account") or "").strip()
    if account_id:
        # A mangled choice is the user's problem to fix, not a server error.
        try:
            uuid.UUID(account_id)
        except ValueError:
            bank_account = None
        else:
            bank_account = BankAccount.objects.filter(
                pk=account_id, company=request.active_company, is_active=True).first()
        if bank_account is None:
            messages.error(
                request,
                "The account to pay from is not one of this company's bank accounts. "
                "Choose it from the list and try again.")
            return redirect("expenses:claim_detail", pk=pk)

    payment_date = timezone.localdate()
    raw_date = (request.POST.get("payment_date") or "").strip()
    if raw_date:
        try:
            payment_date = datetime.date.fromisoformat(raw_date)
        except ValueError:
            messages.error(
                request,
                f"'{raw_date}' is not a date. Enter the day the money was paid as "
                "YYYY-MM-DD, or leave it blank for today.")
            return redirect("expenses:claim_detail", pk=pk)
    # Money is paid back after it was claimed, and on a day that has happened:
    # a payment dated before the claim, or next month, books a bank movement
    # the bank statement will never show.
    if payment_date < claim.claim_date:
        messages.error(
            request,
            f"{claim.reference} was claimed on {claim.claim_date:%d %b %Y}, so it cannot "
            f"have been paid on {payment_date:%d %b %Y}. Enter the day the money "
            "actually left — on or after the claim date.")
        return redirect("expenses:claim_detail", pk=pk)
    if payment_date > timezone.localdate():
        messages.error(
            request,
            f"{payment_date:%d %b %Y} is in the future. Record the payment on the day "
            "the money leaves — today, or an earlier day if it was paid already.")
        return redirect("expenses:claim_detail", pk=pk)

    from django.core.exceptions import ValidationError
    from apps.accounting.gl_service import post_expense_claim
    try:
        with transaction.atomic():
            entry = post_expense_claim(claim, bank_account=bank_account, user=request.user,
                                       payment_date=payment_date)
            type(claim).objects.filter(pk=claim.pk).update(paid_by=request.user)
    except ValidationError as exc:
        messages.error(request, f"{claim.reference} was not paid: {exc.messages[0]}")
    except Exception as exc:
        messages.error(request, f"GL posting failed: {exc}")
    else:
        messages.success(request, f"Claim {claim.reference} paid and posted to GL.")
        # The same warning payroll gives: a payment dated into a closed month
        # is allowed — late paperwork happens — but it is said out loud.
        if entry.calendar_warning:
            messages.warning(request, entry.calendar_warning)
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
