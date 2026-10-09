"""Global template context processors."""
from apps.organizations.models import Company


def global_context(request):
    ctx = {
        "active_company": getattr(request, "active_company", None),
        "active_branch": getattr(request, "active_branch", None),
        "app_name": "MERP",
        "app_version": "1.0.0",
    }

    if request.user.is_authenticated:
        try:
            ctx["user_companies"] = Company.objects.filter(
                companymembership__user=request.user,
                is_active=True,
            ).distinct()
        except Exception:
            ctx["user_companies"] = []

    return ctx
