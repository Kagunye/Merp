from django.apps import AppConfig


class HrConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.hr"
    verbose_name = "Hr"

    def ready(self):
        # A new company gets the standard leave types the moment it exists, so
        # the first person to ask for leave is not met with an empty list. The
        # hook lives here rather than in the company set-up service so the
        # leave rules stay inside the HR app.
        from django.db.models.signals import post_save

        from apps.organizations.models import Company

        post_save.connect(_seed_leave_types, sender=Company,
                          dispatch_uid="hr.seed_leave_types")


def _seed_leave_types(sender, instance, created, raw=False, **kwargs):
    if not created or raw:
        return
    from .models import ensure_default_leave_types

    ensure_default_leave_types(instance)
