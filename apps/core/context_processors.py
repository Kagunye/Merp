"""Global template context processors."""
from apps.organizations.models import Company


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
                companymembership__user=request.user,
                is_active=True,
            ).distinct()
        except Exception:
            ctx["user_companies"] = []

    return ctx


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
