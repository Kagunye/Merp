import re

from django import forms

from .models import TagBatch
from .services import MAX_BATCH


def split_names(text):
    """One name per line, in number order; a blank line leaves that tag unnamed."""
    lines = [ln.strip()[:80] for ln in (text or "").splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return lines


class TagBatchForm(forms.ModelForm):
    start = forms.IntegerField(
        label="Start number", required=False, min_value=1,
        help_text="Blank continues from the last number issued with this prefix.")
    names = forms.CharField(
        label="Tag names", required=False, widget=forms.Textarea(attrs={"rows": 5, "placeholder": "Reception laptop\nFinance laptop\nBoardroom projector"}),
        help_text="Optional: what you call each tag, one per line in number order. It prints on the label "
                  "and goes into the QR. Leave a line blank to skip a tag; rename any tag later.")

    class Meta:
        model = TagBatch
        fields = ["title", "purpose", "product", "asset", "subject_text", "prefix", "padding", "start",
                  "quantity", "label_size", "show_logo", "show_company", "code_type", "qr_content",
                  "show_code", "show_price", "line1", "line2", "names"]
        widgets = {"padding": forms.NumberInput(attrs={"min": 1, "max": 9}),
                   "quantity": forms.NumberInput(attrs={"min": 1, "max": MAX_BATCH})}

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.fixed_assets.models import FixedAsset
        from apps.inventory.models import Product
        self.company = company
        self.fields["product"].queryset = Product.objects.filter(company=company, is_active=True).order_by("name")
        self.fields["asset"].queryset = FixedAsset.objects.filter(company=company, is_active=True).order_by("code")
        self.fields["product"].label_from_instance = lambda p: f"{p.sku} — {p.name}"
        self.fields["asset"].label_from_instance = lambda a: f"{a.code} — {a.name}"
        self.fields["quantity"].help_text = f"How many tags (up to {MAX_BATCH})."
        self.fields["prefix"].help_text = "Letters, digits and - / . only, e.g. TAG- or AST/."

    def clean_prefix(self):
        value = (self.cleaned_data.get("prefix") or "").strip().upper()
        if not value or not re.fullmatch(r"[A-Z0-9\-/.]{1,12}", value):
            raise forms.ValidationError("Use 1 to 12 letters, digits, - / or .")
        return value

    def clean_padding(self):
        value = self.cleaned_data.get("padding") or 5
        if not 1 <= value <= 9:
            raise forms.ValidationError("Between 1 and 9 digits.")
        return value

    def clean_quantity(self):
        value = self.cleaned_data.get("quantity")
        if not value or not 1 <= value <= MAX_BATCH:
            raise forms.ValidationError(f"Between 1 and {MAX_BATCH}.")
        return value

    def clean(self):
        data = super().clean()
        purpose = data.get("purpose")
        if purpose == "PRODUCT" and not data.get("product"):
            self.add_error("product", "Choose the product these tags are for.")
        if purpose == "ASSET" and not data.get("asset"):
            self.add_error("asset", "Choose the fixed asset these tags are for.")
        if purpose != "PRODUCT":
            data["product"] = None
            data["show_price"] = False
        if purpose != "ASSET":
            data["asset"] = None
        names = split_names(data.get("names"))
        if data.get("quantity") and len(names) > data["quantity"]:
            self.add_error("names", f"{len(names)} names for {data['quantity']} tags. Give one name per tag at most.")
        data["names"] = names
        return data
