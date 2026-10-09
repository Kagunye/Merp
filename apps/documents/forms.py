from django import forms
from .models import Document, DocumentCategory


class DocumentCategoryForm(forms.ModelForm):
    class Meta:
        model = DocumentCategory
        fields = ["name", "description"]


class DocumentForm(forms.ModelForm):
    class Meta:
        model = Document
        fields = ["name", "category", "file", "description", "expiry_date"]
        widgets = {"expiry_date": forms.DateInput(attrs={"type": "date"})}
