import datetime

from django import forms
from apps.core.forms import date_input, is_new

from .models import ExpenseCategory, ExpenseClaim, ExpenseClaimLine


class ExpenseCategoryForm(forms.ModelForm):
    class Meta:
        model = ExpenseCategory
        fields = ["name", "gl_account"]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.accounting.models import Account
        self.company = company
        self.fields["gl_account"].queryset = Account.objects.filter(
            company=company, is_active=True, account_type="EXPENSE"
        )
        self.fields["gl_account"].empty_label = "— Post to the default expense account (6000)"

    def clean_name(self):
        # The company is set by the view, not the form, so the database's
        # (company, name) uniqueness is never checked by the form itself — a
        # second "Travel" would reach the database and come back as a server
        # error. Two categories of one name would also split one kind of spend
        # across two lines of every report, so it is refused here, by name.
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("Give the category a name, e.g. Travel.")
        if self.company is not None:
            clash = ExpenseCategory.objects.filter(company=self.company, name__iexact=name)
            if not is_new(self):
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise forms.ValidationError(
                    f"There is already a category called '{clash.first().name}'. Use that "
                    "one, or give this one a different name.")
        return name


class ExpenseClaimForm(forms.ModelForm):
    class Meta:
        model = ExpenseClaim
        fields = ["reference", "employee", "claim_date", "notes"]
        widgets = {
            "claim_date": date_input(),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, company=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.hr.access import manages_claims, own_employee
        from apps.hr.models import Employee
        staff = Employee.objects.filter(company=company, is_active=True)
        # A claim is money paid back to the person named on it. Anybody whose
        # job is not finance or HR may only name themselves: a clerk who can
        # raise a claim in a colleague's name can draw cash the colleague never
        # spent. A hand-made post naming somebody else fails as an invalid
        # choice, the same as a name off the list.
        if company is not None and not manages_claims(user, company):
            mine = own_employee(user, company)
            staff = staff.filter(pk=mine.pk) if mine else Employee.objects.none()
            if mine and is_new(self):
                self.fields["employee"].initial = mine.pk
        self.fields["employee"].queryset = staff
        if is_new(self):
            self.fields["claim_date"].initial = datetime.date.today()

    def clean_claim_date(self):
        """A claim is handed in for money already spent, on a day that has come.

        A claim dated next year sorts and reports in a month that has not
        happened, and every receipt on it would then be "before the claim"
        whatever its date. Nor may an edit move the claim to before a line
        already on it: a line is never dated after the claim it is part of.
        """
        from django.utils import timezone

        day = self.cleaned_data.get("claim_date")
        if day is None:
            return day
        today = timezone.localdate()
        if day > today:
            raise forms.ValidationError(
                f"{day:%d %b %Y} is in the future. A claim is dated the day it is handed "
                f"in — today ({today:%d %b %Y}) or earlier.")
        if not is_new(self):
            latest = (self.instance.lines.order_by("-expense_date")
                      .values_list("expense_date", flat=True).first())
            if latest and latest > day:
                raise forms.ValidationError(
                    f"This claim has a line dated {latest:%d %b %Y}, after "
                    f"{day:%d %b %Y}. A claim cannot be dated before the spending on it — "
                    "keep a later claim date, or correct the line first.")
        return day


class ExpenseClaimLineForm(forms.ModelForm):
    class Meta:
        model = ExpenseClaimLine
        fields = ["category", "description", "expense_date", "amount", "receipt"]
        widgets = {"expense_date": date_input()}

    def __init__(self, *args, company=None, claim=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.claim = claim
        self.fields["category"].queryset = ExpenseCategory.objects.filter(company=company)
        if not self.is_bound and not self.fields["expense_date"].initial:
            self.fields["expense_date"].initial = datetime.date.today()

    def clean_expense_date(self):
        """Money is claimed back after it was spent, never before.

        A line dated after the claim it is on — or after today — is money the
        claimant has not spent yet: approving it pays out for a receipt that
        cannot exist. It is refused, and the claimant told which date to use.
        """
        from django.utils import timezone

        day = self.cleaned_data.get("expense_date")
        if day is None:
            return day
        today = timezone.localdate()
        if self.claim is not None and self.claim.claim_date and day > self.claim.claim_date:
            raise forms.ValidationError(
                f"This line is dated {day:%d %b %Y}, after the claim's own date of "
                f"{self.claim.claim_date:%d %b %Y}. A claim is for money already spent — "
                "enter the day on the receipt, on or before the claim date.")
        if day > today:
            raise forms.ValidationError(
                f"{day:%d %b %Y} is in the future. Enter the day on the receipt — today "
                "or earlier.")
        return day

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("Line amount must be greater than zero.")
        return amount
