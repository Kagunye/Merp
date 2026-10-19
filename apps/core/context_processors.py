"""Global template context processors."""
from apps.organizations.models import Company


def global_context(request):
    ctx = {
        "active_company": getattr(request, "active_company", None),
        "active_branch": getattr(request, "active_branch", None),
        "app_name": "Mofisi",
        "app_version": "1.0.0",
    }

    if request.user.is_authenticated:
        ctx["user_companies"] = Company.objects.filter(
            memberships__user=request.user,
            memberships__is_active=True,
            is_active=True,
        ).distinct()

    return ctx
