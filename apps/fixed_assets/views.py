"""Fixed Assets views."""
import datetime
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import CreateView, DetailView, FormView, ListView

from .forms import AssetCategoryForm, AssetDisposalForm, DepreciationRunForm, FixedAssetForm
from .models import AssetCategory, AssetDepreciation, AssetDisposal, FixedAsset


class AssetCategoryListView(LoginRequiredMixin, ListView):
    template_name = "fixed_assets/category_list.html"
    context_object_name = "categories"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return AssetCategory.objects.none()
        return AssetCategory.objects.filter(company=company, is_active=True)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Asset Categories"
        return ctx


class AssetCategoryCreateView(LoginRequiredMixin, CreateView):
    model = AssetCategory
    form_class = AssetCategoryForm
    template_name = "fixed_assets/category_form.html"

    def get_success_url(self):
        from django.urls import reverse
        return reverse("fixed_assets:category_list")

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        company = self.request.active_company
        if company:
            from apps.accounting.models import Account
            accounts = Account.objects.filter(company=company, is_active=True)
            form.fields["gl_asset_account"].queryset = accounts
            form.fields["gl_depreciation_account"].queryset = accounts
            form.fields["gl_accumulated_account"].queryset = accounts
        return form

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        messages.success(self.request, f"Category '{form.instance.name}' created.")
        return super().form_valid(form)

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
        qs = FixedAsset.objects.filter(company=company, is_active=True).select_related("category")
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


class FixedAssetCreateView(LoginRequiredMixin, CreateView):
    model = FixedAsset
    form_class = FixedAssetForm
    template_name = "fixed_assets/asset_form.html"

    def get_success_url(self):
        from django.urls import reverse
        return reverse("fixed_assets:asset_detail", kwargs={"pk": self.object.pk})

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        company = self.request.active_company
        if company:
            form.fields["category"].queryset = AssetCategory.objects.filter(company=company, is_active=True)
            from apps.crm.models import Supplier
            form.fields["supplier"].queryset = Supplier.objects.filter(company=company, is_active=True)
            # Generate auto code
            from apps.core.models import SequenceCounter
            form.fields["code"].initial = SequenceCounter.next_number(company, "FA-")
        import datetime
        form.fields["purchase_date"].initial = datetime.date.today()
        return form

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        messages.success(self.request, f"Asset '{form.instance.name}' created.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "New Fixed Asset"
        return ctx


class FixedAssetDetailView(LoginRequiredMixin, DetailView):
    model = FixedAsset
    template_name = "fixed_assets/asset_detail.html"
    context_object_name = "asset"

    def get_queryset(self):
        return FixedAsset.objects.filter(company=self.request.active_company)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Asset: {self.object.code}"
        ctx["depreciations"] = self.object.depreciations.select_related("journal_entry").order_by("-period_date")
        ctx["disposal"] = getattr(self.object, "disposal", None)
        return ctx


class DepreciationRunView(LoginRequiredMixin, FormView):
    """Run depreciation for a given period month for all active assets."""
    form_class = DepreciationRunForm
    template_name = "fixed_assets/depreciation_form.html"

    def get_success_url(self):
        from django.urls import reverse
        return reverse("fixed_assets:asset_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Run Depreciation"
        ctx["asset"] = get_object_or_404(
            FixedAsset, pk=self.kwargs["pk"], company=self.request.active_company
        )
        return ctx

    def form_valid(self, form):
        period_date = form.cleaned_data["period_date"]
        asset = get_object_or_404(
            FixedAsset, pk=self.kwargs["pk"], company=self.request.active_company
        )
        if asset.status != "ACTIVE":
            messages.error(self.request, "Only active assets can be depreciated.")
            return self.form_invalid(form)
        # Check not already done for this period
        if asset.depreciations.filter(period_date=period_date).exists():
            messages.error(self.request, f"Depreciation for {period_date} already exists.")
            return self.form_invalid(form)
        # Calculate depreciation amount
        if asset.depreciation_method == "SLM":
            amount = asset.monthly_depreciation_slm
        else:  # WDV
            monthly_rate = Decimal("1") - (asset.residual_value / asset.purchase_cost) ** (
                Decimal("1") / (Decimal(str(asset.useful_life_years)) * 12)
            ) if asset.purchase_cost > 0 else Decimal("0")
            amount = asset.book_value * monthly_rate

        # Cap at remaining depreciable amount
        remaining = asset.purchase_cost - asset.residual_value - asset.accumulated_depreciation
        if remaining <= 0:
            messages.warning(self.request, "Asset is fully depreciated.")
            return self.form_invalid(form)
        amount = min(amount, remaining)
        amount = amount.quantize(Decimal("0.01"))

        new_accumulated = asset.accumulated_depreciation + amount
        new_book_value = asset.purchase_cost - new_accumulated

        with transaction.atomic():
            AssetDepreciation.objects.create(
                asset=asset,
                period_date=period_date,
                depreciation_amount=amount,
                accumulated_depreciation=new_accumulated,
                book_value_after=new_book_value,
            )
            asset.accumulated_depreciation = new_accumulated
            asset.book_value = new_book_value
            asset.last_depreciated = period_date
            asset.save(update_fields=["accumulated_depreciation", "book_value", "last_depreciated", "updated_at"])

        messages.success(self.request, f"Depreciation of {amount:,.2f} posted for {period_date}.")
        return super().form_valid(form)


class AssetDisposalCreateView(LoginRequiredMixin, CreateView):
    model = AssetDisposal
    form_class = AssetDisposalForm
    template_name = "fixed_assets/disposal_form.html"

    def get_success_url(self):
        from django.urls import reverse
        return reverse("fixed_assets:asset_detail", kwargs={"pk": self.kwargs["pk"]})

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["asset"] = get_object_or_404(
            FixedAsset, pk=self.kwargs["pk"], company=self.request.active_company
        )
        ctx["page_title"] = f"Dispose Asset: {ctx['asset'].code}"
        return ctx

    def form_valid(self, form):
        asset = get_object_or_404(
            FixedAsset, pk=self.kwargs["pk"], company=self.request.active_company
        )
        if hasattr(asset, "disposal"):
            messages.error(self.request, "This asset has already been disposed.")
            from django.urls import reverse
            return redirect(reverse("fixed_assets:asset_detail", kwargs={"pk": asset.pk}))
        form.instance.asset = asset
        proceeds = form.cleaned_data["proceeds"]
        form.instance.gain_loss = proceeds - asset.book_value
        with transaction.atomic():
            disposal = form.save()
            asset.status = "DISPOSED"
            asset.save(update_fields=["status", "updated_at"])
        messages.success(self.request, f"Asset {asset.code} disposed. Gain/Loss: {disposal.gain_loss:,.2f}")
        return redirect(self.get_success_url())
