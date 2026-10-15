"""Workshop module landing views."""
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView


class WorkshopIndexView(LoginRequiredMixin, TemplateView):
    """Service / workshop job launcher — invoices, quotations, scheduling."""
    template_name = "workshop/index.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if not company:
            return ctx
        from apps.sales.models import Invoice
        from apps.helpdesk.models import Ticket
        from apps.hr.models import Employee
        ctx["open_jobs"] = Ticket.objects.filter(company=company, status__in=["OPEN", "IN_PROGRESS"]).count()
        ctx["invoiced_jobs"] = Invoice.objects.filter(company=company).count()
        ctx["technicians"] = Employee.objects.filter(company=company, is_active=True).count()
        return ctx
