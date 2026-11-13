"""Global template context processors."""
from apps.organizations.models import Company, CompanyMembership

ROLE_CHOICES = CompanyMembership.ROLE_CHOICES


def global_context(request):
    ctx = {
        "active_company": getattr(request, "active_company", None),
        "active_branch": getattr(request, "active_branch", None),
        "app_name": "Mofisi",
        "app_version": "1.0.0",
    }

    if request.user.is_authenticated:
        from apps.core import access
        ctx["user_companies"] = Company.objects.filter(
            memberships__user=request.user,
            memberships__is_active=True,
            is_active=True,
        ).distinct()
        role = getattr(request, "company_role", None)
        # Which modules this person may open: the sidebar hides the rest.
        ctx["nav"] = {ns: access.can_view_module(request.user, role, ns) for ns in NAV_MODULES}
        ctx["company_role"] = role
        ctx["company_role_label"] = dict(ROLE_CHOICES).get(role, "") if role else ""
        ctx["can_manage_users"] = access.can(request.user, role, "accounts", "user_list")
        # Display only: the branch switcher in the top bar, and the style-guide
        # link in the user menu. Lazy — no query unless a template reads it.
        company = ctx["active_company"]
        if company is not None:
            from apps.organizations.models import Branch
            ctx["user_branches"] = Branch.objects.filter(company=company, is_active=True).order_by("name")
        ctx["is_platform_admin"] = access.is_platform_admin(request.user)

    return ctx


NAV_MODULES = [
    "dashboard", "accounting", "business_mgmt", "banking", "crm", "sales", "pos", "kra", "inventory",
    "procurement", "warehousing", "manufacturing", "workshop", "hr", "payroll", "timesheets",
    "recruitment", "projects", "expenses", "fixed_assets", "reporting", "documents", "helpdesk",
    "notifications", "sms", "control_panel",
]
