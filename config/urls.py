"""Root URL configuration."""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic import TemplateView
from django.views.decorators.cache import never_cache


# Service worker must be served from the site root so it can control / scope.
sw_view = never_cache(TemplateView.as_view(
    template_name="sw.js", content_type="application/javascript"
))
offline_view = TemplateView.as_view(template_name="offline.html")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("sw.js", sw_view, name="service_worker"),
    path("offline/", offline_view, name="offline"),
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

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    try:
        import debug_toolbar
        urlpatterns = [path("__debug__/", include(debug_toolbar.urls))] + urlpatterns
    except ImportError:
        pass
