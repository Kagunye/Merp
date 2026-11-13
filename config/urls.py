"""Root URL configuration."""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.accounts.views import throttled_admin_login

# The Django admin's sign-in form shares the sign-in page's lockout: five
# failures for one account or one address within 15 minutes pause it, and
# every attempt is recorded (apps.accounts.views.throttled_admin_login).
# Set before admin.site.urls is read, which is when the route is bound.
if not getattr(admin.site.login, "_merp_throttled", False):
    admin.site.login = throttled_admin_login(admin.site.login)
    admin.site.login._merp_throttled = True

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("apps.core.urls")),
    path("auth/", include("apps.accounts.urls")),
    path("dashboard/", include("apps.core.dashboard_urls")),
    path("organizations/", include("apps.organizations.urls")),
    path("accounting/", include("apps.accounting.urls")),
    path("banking/", include("apps.banking.urls")),
    path("inventory/", include("apps.inventory.urls")),
    path("warehousing/", include("apps.warehousing.urls")),
    path("workshop/", include("apps.workshop.urls")),
    path("business-management/", include("apps.business_mgmt.urls")),
    path("sms/", include("apps.sms.urls")),
    path("control-panel/", include("apps.control_panel.urls")),
    path("sales/", include("apps.sales.urls")),
    path("crm/", include("apps.crm.urls")),
    path("procurement/", include("apps.procurement.urls")),
    path("hr/", include("apps.hr.urls")),
    path("payroll/", include("apps.payroll.urls")),
    path("projects/", include("apps.projects.urls")),
    path("expenses/", include("apps.expenses.urls")),
    path("pos/", include("apps.pos.urls")),
    path("kra/", include("apps.kra.urls")),
    path("documents/", include("apps.documents.urls")),
    path("reports/", include("apps.reporting.urls")),
    path("fixed-assets/", include("apps.fixed_assets.urls")),
    path("manufacturing/", include("apps.manufacturing.urls")),
    path("notifications/", include("apps.notifications.urls")),
    path("timesheets/", include("apps.timesheets.urls")),
    path("helpdesk/", include("apps.helpdesk.urls")),
    path("recruitment/", include("apps.recruitment.urls")),
    path("api/v1/", include("apps.api.urls")),
]

try:
    from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
    urlpatterns += [
        path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
        path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    ]
except ImportError:
    pass

# Uploaded files are NOT served from /media/. A plain media route can't tell
# who is asking: it would hand one company's contracts, CVs and receipts to
# anybody holding the link. Each kind of upload has its own view that checks
# the user's company and rights and sends the file as an attachment
# (documents:document_download, documents:receipt_download,
# recruitment:candidate_resume).
#
# The only exception, and only with DEBUG on (production has no media route
# at all), is pictures that are shown inline on pages -- avatars, company and
# customer logos, product images -- which are not confidential and which a
# development server has no other way to show.
#
# config.media_views.public_media serves them: it resolves the path and
# refuses anything outside MEDIA_ROOT/<public prefix>, any "." or ".."
# segment however it was encoded, and anything that is not a picture.
from config.media_views import PUBLIC_MEDIA_PREFIXES  # noqa: E402  (re-exported)

if settings.DEBUG:
    import re as _re

    from django.urls import re_path

    from config.media_views import public_media as _public_media

    _media = _re.escape(settings.MEDIA_URL.lstrip("/"))
    _prefixes = "|".join(_re.escape(p) for p in PUBLIC_MEDIA_PREFIXES)
    urlpatterns += [
        re_path(rf"^{_media}(?P<path>(?:{_prefixes}).+)$", _public_media, name="public_media"),
    ]
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    try:
        import debug_toolbar
        urlpatterns = [path("__debug__/", include(debug_toolbar.urls))] + urlpatterns
    except ImportError:
        pass

handler500 = "apps.core.errors.server_error"
