"""Global template context processors."""
from apps.organizations.models import Company

from . import access


def global_context(request):
    active = getattr(request, "active_company", None)

    # Fallback brand palette (mofisi red) when there's no active company yet.
    brand_primary = "#DC2538"
    brand_accent = "#111827"
    if active:
        brand_primary = getattr(active, "primary_color", "") or brand_primary
        brand_accent = getattr(active, "accent_color", "") or brand_accent

    ctx = {
        "active_company": active,
        "active_branch": getattr(request, "active_branch", None),
        "app_name": "MERP",
        "app_version": "1.0.0",
        # Theme — read inside base.html and shared document template.
        "brand_primary": brand_primary,
        "brand_accent": brand_accent,
        "brand_primary_dark": _shade(brand_primary, -0.18),
        "brand_primary_light": _shade(brand_primary, 0.18),
    }

    if request.user.is_authenticated:
        try:
            ctx["user_companies"] = Company.objects.filter(
                memberships__user=request.user,
                memberships__is_active=True,
                is_active=True,
            ).distinct()
        except Exception:
            ctx["user_companies"] = []

        role = getattr(request, "company_role", None)
        nav = {ns: access.can_view_module(request.user, role, ns) for ns in NAV_MODULES}
        ctx["nav"] = nav
        ctx["nav_sections"] = {
            section: any(nav[ns] for ns in members) for section, members in NAV_SECTIONS.items()
        }
        ctx["company_role"] = role
        ctx["can_manage_users"] = access.can(request.user, role, "accounts", "user_list")
        ctx["company_role_label"] = (role or "").replace("_", " ").title()

    return ctx


NAV_SECTIONS = {
    "finance": ["accounting", "business_mgmt", "banking"],
    "sales": ["crm", "sales", "pos"],
    "supply_chain": ["inventory", "procurement", "warehousing", "manufacturing", "workshop"],
    "people": ["hr", "payroll", "timesheets", "recruitment"],
    "operations": ["projects", "expenses", "fixed_assets", "reporting", "documents"],
    "support": ["helpdesk", "notifications", "sms", "control_panel"],
}
NAV_MODULES = ["dashboard"] + [ns for members in NAV_SECTIONS.values() for ns in members]


def _shade(hex_color, pct):
    """Return a hex colour shifted by `pct` (−1..+1). Darker < 0 < lighter."""
    try:
        hex_color = hex_color.lstrip("#")
        if len(hex_color) != 6:
            return "#C8102E"
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        if pct < 0:
            r = int(r * (1 + pct)); g = int(g * (1 + pct)); b = int(b * (1 + pct))
        else:
            r = int(r + (255 - r) * pct); g = int(g + (255 - g) * pct); b = int(b + (255 - b) * pct)
        r = max(0, min(255, r)); g = max(0, min(255, g)); b = max(0, min(255, b))
        return f"#{r:02X}{g:02X}{b:02X}"
    except Exception:
        return "#C8102E"
