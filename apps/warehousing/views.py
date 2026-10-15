"""Warehouse Management landing views."""
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView


class WarehousingIndexView(LoginRequiredMixin, TemplateView):
    """Picking, shipping, and warehouse operations launcher."""
    template_name = "warehousing/index.html"

    def get_context_data(self, **kwargs):
        from apps.inventory.models import StockLevel, StockMovement
        from apps.organizations.models import Warehouse
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if not company:
            return ctx
        ctx["warehouse_count"] = Warehouse.objects.filter(company=company, is_active=True).count()
        ctx["recent_movement_count"] = StockMovement.objects.filter(
            warehouse__company=company
        ).count()
        ctx["skus_on_hand"] = StockLevel.objects.filter(
            warehouse__company=company
        ).count()
        return ctx
