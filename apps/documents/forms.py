from django import forms
from apps.core.forms import date_input
from .models import Document, DocumentCategory
from .validators import ALLOWED_UPLOAD_EXTENSIONS, max_upload_size


class DocumentCategoryForm(forms.ModelForm):
    class Meta:
        model = DocumentCategory
        fields = ["name", "description"]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company

    def clean_name(self):
        """One category of each name per company.

        The table enforces it too, but `company` isn't a field on this form,
        so Django's own uniqueness check never sees the pair and a duplicate
        reached the database as an IntegrityError (a 500). Checked here, it
        is a message on the field instead. Case is ignored: "Contracts" and
        "contracts" in one list are the same category to a reader.
        """
        name = (self.cleaned_data.get("name") or "").strip()
        if name and self.company is not None:
            twin = DocumentCategory.objects.filter(
                company=self.company, name__iexact=name).exclude(pk=self.instance.pk).first()
            if twin:
                raise forms.ValidationError(
                    f"There is already a category called '{twin.name}'. "
                    "Use that one, or pick a different name.")
        return name


class DocumentForm(forms.ModelForm):
    class Meta:
        model = Document
        fields = ["name", "category", "file", "description", "expiry_date"]
        widgets = {"expiry_date": date_input()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["file"].help_text = (
            f"Up to {max_upload_size() // (1024 * 1024)} MB. Allowed: "
            + ", ".join(ALLOWED_UPLOAD_EXTENSIONS) + ".")
        self.fields["file"].widget.attrs["accept"] = ",".join(
            "." + ext for ext in ALLOWED_UPLOAD_EXTENSIONS)
