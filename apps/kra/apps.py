from django.apps import AppConfig


class KraConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.kra"
    label = "kra"
    verbose_name = "KRA eTIMS"

    def ready(self):
        from . import signals  # noqa: F401  (connects the document hooks)
