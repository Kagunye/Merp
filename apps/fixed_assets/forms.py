"""Fixed asset forms.

The register holds the price, the life and the accounts; the charge each month
is worked out from those three and never typed. So these forms validate what a
depreciation run depends on — a life of zero has no monthly figure to post, and
an account from another book would post into a ledger this company does not own.
"""
from django import forms
from django.utils import timezone

from apps.core.forms import date_input, is_new
from .models import AssetCategory, AssetDisposal, FixedAsset

FIELD_CLASS = "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"


class BookScopedForm(forms.ModelForm):
    """Shared plumbing for the forms that name accounts, categories and suppliers.

    Every queryset is cut to the book being worked in, and a new row is given its
    owner before it is validated: the model's own checks read `company`, and a
    form validating an ownerless draft would wave through a clash.
    """

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        if not company:
            return
        # A new row is given its owner before it is validated: the model's own
        # checks read `company`, and a form validating an ownerless draft would
        # wave through a clash. hasattr cannot be used to find the field — a
        # required foreign key that is still empty raises on access, so a fresh
        # AssetCategory reports having no company until one is assigned.
        if is_new(self) and any(f.name == "company"
                                for f in self.instance._meta.fields):
            self.instance.company = company
        self.scope(company)

    def scope(self, company):
        pass


class AssetCategoryForm(BookScopedForm):
    class Meta:
        model = AssetCategory
        fields = ["name", "default_useful_life", "default_depreciation_method",
                  "gl_asset_account", "gl_depreciation_account",
                  "gl_accumulated_account"]
        help_texts = {
            "default_useful_life": "Years an asset in this category is expected to "
                                   "serve. Assets can override it individually.",
        }

    def scope(self, company):
        from apps.accounting.models import Account
        by_type = lambda kind: Account.objects.filter(  # noqa: E731
            company=company, is_active=True, account_type=kind).order_by("code")
        self.fields["gl_asset_account"].queryset = by_type("ASSET")
        # Accumulated depreciation is a contra asset — it sits inside the asset
        # side of the balance sheet and carries a credit — so it is chosen from
        # the asset accounts, not the expense ones.
        self.fields["gl_accumulated_account"].queryset = by_type("ASSET")
        self.fields["gl_depreciation_account"].queryset = by_type("EXPENSE")
        for name in ("gl_asset_account", "gl_depreciation_account",
                     "gl_accumulated_account"):
            self.fields[name].empty_label = "— Use the book's default"

    def clean(self):
        cleaned = super().clean()
        if self.company and cleaned.get("name"):
            clash = AssetCategory.objects.filter(
                company=self.company, name__iexact=cleaned["name"])
            if not is_new(self):
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                self.add_error(
                    "name", f"A category called '{cleaned['name']}' already exists "
                    "in this book. Two of them would split the same assets across "
                    "two sets of accounts.")
        return cleaned


class FixedAssetForm(BookScopedForm):
    class Meta:
        model = FixedAsset
        # Status is not on the form. Each change of status is a ledger event —
        # a disposal or write-off clears the cost and the wear, standing the
        # purchase down takes the cost back out — and each has its own screen
        # that posts the entry. A status typed here moved the register and left
        # the ledger behind.
        fields = ["code", "name", "category", "serial_number", "location",
                  "purchase_date", "purchase_cost", "residual_value",
                  "useful_life_years", "depreciation_method",
                  "supplier", "paid_into", "warranty_expiry", "notes"]
        widgets = {
            "purchase_date": date_input(),
            "warranty_expiry": date_input(),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "purchase_cost": "What the asset cost, in this book's currency. The "
                            "ledger is charged this figure as soon as the asset "
                            "is saved.",
            "residual_value": "What it is expected to be worth at the end of its "
                              "life. Depreciation stops here.",
            "paid_into": "The bank or drawer the purchase was paid out of.",
        }

    def scope(self, company):
        from apps.accounting.models import Account
        from apps.crm.models import Supplier
        self.fields["category"].queryset = AssetCategory.objects.filter(
            company=company, is_active=True)
        self.fields["supplier"].queryset = Supplier.objects.filter(
            company=company, is_active=True)
        self.fields["paid_into"].queryset = Account.objects.filter(
            company=company, is_active=True,
            account_type__in=["BANK", "CASH"]).order_by("code")
        self.fields["paid_into"].empty_label = "— 1200 Bank (default)"
        if is_new(self):
            # A preview only: the number is taken when the asset is saved, so
            # opening the form and walking away leaves no gap in the FA- series.
            from apps.core.models import SequenceCounter
            self.fields["code"].initial = SequenceCounter.peek_number(company, "FA-")
            return

        asset = self.instance
        disposal = getattr(asset, "disposal", None)
        charged = any(row.journal_entry_id and not row.journal_entry.reversed_by_id
                      for row in asset.depreciations.select_related("journal_entry"))
        if asset.acquisition_is_live or charged \
                or (disposal is not None and not disposal.is_reversed):
            # The category decides which cost, wear and expense accounts the
            # asset posts to, and paid_into which bank paid for it. Changing
            # either under a posted purchase would leave the cost on one
            # account while every later entry credits another — one account
            # left holding the asset for ever, the other driven below nothing.
            for name in ("purchase_date", "purchase_cost", "residual_value",
                         "useful_life_years", "depreciation_method",
                         "category", "paid_into"):
                self.fields[name].disabled = True
                self.fields[name].help_text = (
                    "Already fed to the ledger, so it cannot be changed here. "
                    "Stand the purchase (and any charges) down from the asset's "
                    "page first, then correct it and save to book it again.")

    def clean(self):
        cleaned = super().clean()
        if self.company and cleaned.get("code"):
            clash = FixedAsset.objects.filter(company=self.company,
                                              code__iexact=cleaned["code"])
            if not is_new(self):
                clash = clash.exclude(pk=self.instance.pk)
            from apps.core.models import SequenceCounter
            preview = SequenceCounter.peek_number(self.company, "FA-")
            if clash.exists() and is_new(self) and self._is_series(cleaned["code"]):
                # The form was opened with a preview a colleague has since used:
                # that is not the user's mistake, so the asset simply takes the
                # next free number instead of being sent back.
                cleaned["code"] = preview
            elif clash.exists():
                self.add_error(
                    "code", f"{cleaned['code']} is already on the register. Two "
                    "assets under one code cannot be told apart in the ledger.")
        return cleaned

    @staticmethod
    def _is_series(code):
        rest = code[3:] if code.upper().startswith("FA-") else ""
        return rest.isdigit()

    def save(self, commit=True):
        # The FA- number is taken now, not when the form was opened. The code
        # shown is kept if it is still free; if a colleague saved an asset under
        # it in the meantime, the next free number is used instead.
        if is_new(self) and self.company:
            from apps.core.models import SequenceCounter
            company = self.company
            self.instance.code = SequenceCounter.claim_number(
                company, "FA-", self.instance.code,
                taken=lambda ref: FixedAsset.objects.filter(
                    company=company, code__iexact=ref).exists())
        return super().save(commit)


class AssetDisposalForm(BookScopedForm):
    class Meta:
        model = AssetDisposal
        fields = ["disposal_date", "disposal_type", "proceeds", "received_into",
                  "notes"]
        widgets = {
            "disposal_date": date_input(),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "proceeds": "What was actually received. Zero for something scrapped.",
            "received_into": "The bank or drawer the money came into.",
        }

    def __init__(self, *args, company=None, asset=None, **kwargs):
        super().__init__(*args, company=company, **kwargs)
        # The asset is on the instance before validation: the model's checks —
        # could it have gone before it arrived, has the book reached the value
        # the disposal clears — all read through it.
        if asset is not None:
            self.instance.asset = asset

    def scope(self, company):
        from apps.accounting.models import Account
        self.fields["received_into"].queryset = Account.objects.filter(
            company=company, is_active=True,
            account_type__in=["BANK", "CASH"]).order_by("code")
        self.fields["received_into"].empty_label = "— 1200 Bank (default)"
        if is_new(self):
            self.fields["disposal_date"].initial = timezone.localdate()


class DepreciationRunForm(forms.Form):
    period_date = forms.DateField(
        widget=date_input({"class": FIELD_CLASS}),
        help_text="The month being charged. Use the first day of it, and work "
                  "through the months in order.")
