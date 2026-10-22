"""Fixed Assets views.

An asset is only a spreadsheet row until the ledger carries it, so saving one
posts its acquisition, running depreciation posts the month's charge, and
disposing posts the entry that clears both the cost and the wear. Nothing here
edits a posted figure: a mistake is answered by the reversal next to it.
"""
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import (
    CreateView, DetailView, FormView, ListView, TemplateView, UpdateView,
)

from . import services
from .forms import (
    AssetCategoryForm, AssetDisposalForm, DepreciationRunForm, FixedAssetForm,
)
from .models import AssetCategory, AssetDepreciation, AssetDisposal, FixedAsset


def _asset(request, pk):
    return get_object_or_404(FixedAsset, pk=pk, company=request.active_company)


def _posted_as(entry):
    """The lines an entry moved, in the words an accountant checks them in."""
    return " — " + ", ".join(
        f"{'Dr' if line.debit_amount else 'Cr'} {line.account.code} "
        f"{line.account.name} {(line.debit_amount or line.credit_amount):,.2f}"
        for line in entry.lines.select_related("account").order_by("account__code")
    )


class AssetCategoryListView(LoginRequiredMixin, ListView):
    template_name = "fixed_assets/category_list.html"
    context_object_name = "categories"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return AssetCategory.objects.none()
        return AssetCategory.objects.filter(
            company=company, is_active=True).select_related(
            "gl_asset_account", "gl_depreciation_account", "gl_accumulated_account")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Asset Categories"
        return ctx


class AssetCategoryCreateView(LoginRequiredMixin, CreateView):
    model = AssetCategory
    form_class = AssetCategoryForm
    template_name = "fixed_assets/category_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_success_url(self):
        return reverse("fixed_assets:category_list")

    def form_valid(self, form):
        category = form.save()
        messages.success(
            self.request,
            f"Category '{category.name}' created. Its assets will be charged over "
            f"{category.default_useful_life} year(s) by default.")
        return redirect(self.get_success_url())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "New Asset Category"
        return ctx


class FixedAssetListView(LoginRequiredMixin, ListView):
    template_name = "fixed_assets/asset_list.html"
    context_object_name = "assets"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return FixedAsset.objects.none()
        qs = FixedAsset.objects.filter(company=company, is_active=True).select_related(
            "category", "journal_entry")
        status = self.request.GET.get("status")
        if status:
            qs = qs.filter(status=status)
        q = self.request.GET.get("q")
        if q:
            from django.db.models import Q
            qs = qs.filter(Q(code__icontains=q) | Q(name__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Fixed Assets Register"
        ctx["status_choices"] = FixedAsset.STATUS_CHOICES
        ctx["status_filter"] = self.request.GET.get("status", "")
        ctx["q"] = self.request.GET.get("q", "")
        return ctx


class AcquisitionPostingMixin:
    """Saving an asset puts it into the ledger, and says where it went.

    An asset the book has never carried — a new one, or an older register row
    saved before the acquisition was ever posted — is brought in here. One that
    has been posted already keeps its entry, and its figures are locked against
    the entries that used them.
    """

    def form_valid(self, form):
        posted_before = self.object.journal_entry_id if self.object else None
        try:
            with transaction.atomic():
                self.object = form.save()
                entry = services.post_acquisition(self.object, user=self.request.user)
        except ValidationError as exc:
            form.add_error(None, exc.messages[0])
            return self.form_invalid(form)
        if posted_before == entry.pk:
            messages.success(self.request, f"{self.object.code} saved.")
        elif posted_before:
            # The purchase had been stood down, and this save brought it back.
            messages.success(
                self.request,
                f"{self.object.code} posted to the ledger again through "
                f"{entry.reference}{_posted_as(entry)}")
        else:
            messages.success(
                self.request,
                f"{self.object.code} added to the register through "
                f"{entry.reference}{_posted_as(entry)}")
        return redirect(self.get_success_url())


class FixedAssetCreateView(LoginRequiredMixin, AcquisitionPostingMixin, CreateView):
    model = FixedAsset
    form_class = FixedAssetForm
    template_name = "fixed_assets/asset_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_success_url(self):
        return reverse("fixed_assets:asset_detail", kwargs={"pk": self.object.pk})

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "New Fixed Asset"
        return ctx


class FixedAssetUpdateView(LoginRequiredMixin, AcquisitionPostingMixin, UpdateView):
    model = FixedAsset
    form_class = FixedAssetForm
    template_name = "fixed_assets/asset_form.html"

    def get_queryset(self):
        return FixedAsset.objects.filter(company=self.request.active_company)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        return kwargs

    def get_success_url(self):
        return reverse("fixed_assets:asset_detail", kwargs={"pk": self.object.pk})

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit Asset: {self.object.code}"
        return ctx


class FixedAssetDetailView(LoginRequiredMixin, DetailView):
    model = FixedAsset
    template_name = "fixed_assets/asset_detail.html"
    context_object_name = "asset"

    def get_queryset(self):
        return FixedAsset.objects.filter(
            company=self.request.active_company).select_related(
            "category", "supplier", "paid_into", "journal_entry")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        asset = self.object
        ctx["page_title"] = f"Asset: {asset.code}"
        ctx["depreciations"] = asset.depreciations.select_related(
            "journal_entry").order_by("-period_date")
        ctx["disposal"] = getattr(asset, "disposal", None)
        ctx["next_period"] = services.next_charge_date(asset)
        ctx["preview"] = services.charge_preview(asset)
        return ctx


class DepreciationRunView(LoginRequiredMixin, FormView):
    """Charge one asset's wear for one month, and post it."""
    form_class = DepreciationRunForm
    template_name = "fixed_assets/depreciation_form.html"

    def get_asset(self):
        return _asset(self.request, self.kwargs["pk"])

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        if not kwargs.get("data"):
            kwargs["initial"] = {"period_date": services.next_charge_date(self.get_asset())}
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        asset = self.get_asset()
        ctx["page_title"] = f"Run Depreciation: {asset.code}"
        ctx["asset"] = asset
        ctx["next_period"] = services.next_charge_date(asset)
        ctx["preview"] = services.charge_preview(asset)
        return ctx

    def form_valid(self, form):
        asset = self.get_asset()
        period_date = form.cleaned_data["period_date"]
        try:
            row = services.depreciate(asset, period_date, user=self.request.user)
        except ValidationError as exc:
            form.add_error("period_date", exc.messages[0])
            messages.error(self.request, exc.messages[0])
            return self.form_invalid(form)
        messages.success(
            self.request,
            f"{asset.code}: {row.depreciation_amount:,.2f} charged for "
            f"{row.period_date:%b %Y} through {row.journal_entry.reference}.")
        return redirect("fixed_assets:asset_detail", pk=asset.pk)


class DepreciationRunAllView(LoginRequiredMixin, FormView):
    """One month across the whole register.

    An asset that cannot be charged is reported, not allowed to abort the run:
    forty vehicles should not fail because one of them arrived last week.
    """
    form_class = DepreciationRunForm
    template_name = "fixed_assets/depreciation_run.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        if not kwargs.get("data"):
            today = timezone.localdate()
            kwargs["initial"] = {"period_date": today.replace(day=1)}
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        ctx["page_title"] = "Run Depreciation"
        active = (FixedAsset.objects.filter(company=company, status="ACTIVE",
                                            is_active=True)
                  .select_related("category").order_by("code"))
        ctx["asset_count"] = active.count()
        ctx["totals"] = active.aggregate(
            gross=Sum("purchase_cost"), net=Sum("book_value"),
            accumulated=Sum("accumulated_depreciation"))
        due = []
        for asset in active:
            period_date = services.next_charge_date(asset)
            if period_date:
                due.append((asset, period_date, services.next_charge_amount(asset)))
        ctx["due"] = due[:20]
        ctx["due_count"] = len(due)
        ctx["due_more"] = max(len(due) - 20, 0)
        ctx["due_total"] = sum((amount for _a, _d, amount in due), Decimal("0"))
        return ctx

    def form_valid(self, form):
        company = self.request.active_company
        period_date = form.cleaned_data["period_date"]
        charged, skipped = services.depreciate_all(
            company, period_date, user=self.request.user)
        total = sum((row.depreciation_amount for _a, row in charged), Decimal("0"))
        if charged:
            messages.success(
                self.request,
                f"{len(charged)} asset(s) charged {total:,.2f} for "
                f"{period_date:%b %Y}.")
        else:
            messages.warning(
                self.request, f"Nothing was charged for {period_date:%b %Y}.")
        if skipped:
            messages.warning(
                self.request,
                f"{len(skipped)} asset(s) were not charged: "
                + ", ".join(a.code for a, _ in skipped[:10])
                + (", …" if len(skipped) > 10 else "")
                + ". Each one says why on its own screen.")
        return redirect("fixed_assets:asset_list")


class AssetDisposalCreateView(LoginRequiredMixin, CreateView):
    template_name = "fixed_assets/disposal_form.html"
    form_class = AssetDisposalForm

    def get_asset(self):
        return _asset(self.request, self.kwargs["pk"])

    def dispatch(self, request, *args, **kwargs):
        asset = self.get_asset()
        existing = getattr(asset, "disposal", None)
        if asset.status != "ACTIVE" or (existing and not existing.is_reversed):
            messages.error(
                request,
                f"{asset.code} is {asset.get_status_display().lower()}, so there is "
                "nothing left to dispose of. Bring the disposal back first if that "
                "was a mistake.")
            return redirect("fixed_assets:asset_detail", pk=asset.pk)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        asset = self.get_asset()
        kwargs["company"] = asset.company
        kwargs["asset"] = asset
        existing = getattr(asset, "disposal", None)
        if existing is not None:
            # The register keeps one disposal row per asset: a stood-down
            # disposal is answered by its reversal in the ledger, so the row is
            # reused and pointed at the new entry rather than duplicated.
            kwargs["instance"] = existing
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        asset = self.get_asset()
        ctx["asset"] = asset
        ctx["page_title"] = f"Dispose Asset: {asset.code}"
        return ctx

    def get_success_url(self):
        return reverse("fixed_assets:asset_detail", kwargs={"pk": self.kwargs["pk"]})

    def form_valid(self, form):
        asset = self.get_asset()
        try:
            with transaction.atomic():
                disposal = form.save()
                entry = services.post_disposal(disposal, user=self.request.user)
        except ValidationError as exc:
            form.add_error(None, exc.messages[0])
            return self.form_invalid(form)
        messages.success(
            self.request,
            f"{asset.code} disposed through {entry.reference}: "
            f"{'gain' if disposal.gain_loss >= 0 else 'loss'} of "
            f"{abs(disposal.gain_loss):,.2f}{_posted_as(entry)}")
        return redirect(self.get_success_url())


@login_required
@require_POST
def acquisition_reverse(request, pk):
    """Unbuy an asset: the cost goes back to the account it was paid out of."""
    asset = _asset(request, pk)
    paid_out_of = (asset.journal_entry.lines.filter(credit_amount__gt=0)
                   .select_related("account").first() if asset.journal_entry_id else None)
    try:
        with transaction.atomic():
            reversal = services.reverse_acquisition(
                asset, user=request.user,
                reason=(request.POST.get("reason") or "").strip())
    except ValidationError as exc:
        messages.error(request, f"Could not stand the acquisition down: {exc.messages[0]}")
        return redirect("fixed_assets:asset_detail", pk=asset.pk)
    messages.success(
        request,
        f"{asset.code}'s purchase was stood down through {reversal.reference}: "
        f"{asset.purchase_cost:,.2f} back on "
        f"{paid_out_of.account if paid_out_of else 'the account it came out of'}.")
    return redirect("fixed_assets:asset_detail", pk=asset.pk)


@login_required
@require_POST
def depreciation_reverse(request, pk):
    """Stand a month's charge down, and put the register back where it was."""
    company = request.active_company
    row = get_object_or_404(
        AssetDepreciation.objects.filter(asset__company=company).select_related(
            "asset", "journal_entry"), pk=pk)
    asset = row.asset
    if not row.journal_entry_id:
        messages.error(
            request, f"{asset.code}'s charge for {row.period_date:%b %Y} was never "
            "posted, so there is nothing in the book to stand down.")
        return redirect("fixed_assets:asset_detail", pk=asset.pk)
    try:
        with transaction.atomic():
            reversal = services.reverse_depreciation(
                row, user=request.user,
                reason=(request.POST.get("reason") or "").strip())
    except ValidationError as exc:
        messages.error(request, f"Could not stand the charge down: {exc.messages[0]}")
        return redirect("fixed_assets:asset_detail", pk=asset.pk)
    messages.success(
        request,
        f"{asset.code}'s charge for {row.period_date:%b %Y} was stood down through "
        f"{reversal.reference}. The book value is back to {asset.book_value:,.2f}.")
    return redirect("fixed_assets:asset_detail", pk=asset.pk)


@login_required
@require_POST
def disposal_reverse(request, pk):
    """Bring a disposed asset back onto the register, at the value it stood at."""
    asset = _asset(request, pk)
    disposal = getattr(asset, "disposal", None)
    if disposal is None or not disposal.journal_entry_id:
        messages.error(
            request,
            f"{asset.code} has no disposal entry in the book to stand down.")
        return redirect("fixed_assets:asset_detail", pk=asset.pk)
    try:
        with transaction.atomic():
            reversal = services.reverse_disposal(
                disposal, user=request.user,
                reason=(request.POST.get("reason") or "").strip())
    except ValidationError as exc:
        messages.error(request, f"Could not bring the asset back: {exc.messages[0]}")
        return redirect("fixed_assets:asset_detail", pk=asset.pk)
    messages.success(
        request,
        f"{asset.code} is active again: {reversal.reference} offsets the disposal, "
        "and the register is back at the figures the disposal cleared.")
    return redirect("fixed_assets:asset_detail", pk=asset.pk)


class FixedAssetsIndexView(LoginRequiredMixin, TemplateView):
    template_name = "fixed_assets/index.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if not company:
            return ctx
        assets = FixedAsset.objects.filter(company=company)
        ctx["asset_count"] = assets.filter(status="ACTIVE").count()
        ctx["disposed_count"] = assets.filter(status="DISPOSED").count()
        ctx["category_count"] = AssetCategory.objects.filter(company=company).count()
        ctx["totals"] = assets.filter(status="ACTIVE").aggregate(
            gross_value=Sum("purchase_cost"),
            net_book_value=Sum("book_value"),
            accumulated=Sum("accumulated_depreciation"))
        ctx["recent_assets"] = assets.select_related("category").order_by(
            "-created_at")[:8]
        return ctx
