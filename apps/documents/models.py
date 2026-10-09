"""Document and file management models."""
from django.db import models
from apps.core.models import BaseModel


class DocumentCategory(BaseModel):
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")

    class Meta:
        unique_together = [("company", "name")]

    def __str__(self):
        return self.name


class Document(BaseModel):
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="documents")
    category = models.ForeignKey(DocumentCategory, on_delete=models.SET_NULL, null=True, blank=True)
    name = models.CharField(max_length=300)
    file = models.FileField(upload_to="documents/%Y/%m/")
    file_size = models.PositiveIntegerField(default=0)
    mime_type = models.CharField(max_length=100, blank=True, default="")
    description = models.TextField(blank=True, default="")
    expiry_date = models.DateField(null=True, blank=True)
    uploaded_by = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, blank=True)
    related_model = models.CharField(max_length=100, blank=True, default="")
    related_id = models.CharField(max_length=50, blank=True, default="")
    version = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    @property
    def file_size_display(self):
        size = self.file_size
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.1f} MB"
