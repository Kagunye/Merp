from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    verbose_name = "Core"

    def ready(self):
        # A company with its own database: route queries and transactions to it.
        from . import tenancy
        tenancy.install()
