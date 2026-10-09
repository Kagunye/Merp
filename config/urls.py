"""Root URL configuration."""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("apps.core.urls")),
    path("auth/", include("apps.accounts.urls")),
    path("dashboard/", include("apps.core.dashboard_urls")),
    path("organizations/", include("apps.organizations.urls")),
    path("accounting/", include("apps.accounting.urls")),
    path("banking/", include("apps.banking.urls")),
    path("inventory/", include("apps.inventory.urls")),
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
