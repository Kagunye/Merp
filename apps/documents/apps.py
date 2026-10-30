from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.documents"
    verbose_name = "Documents"

    def ready(self):
        # Every upload field in the project gets the same type, content and
        # size checks (see apps.documents.validators).
        from .validators import attach_upload_validator
        attach_upload_validator()
