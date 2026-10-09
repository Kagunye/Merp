from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DetailView, ListView, TemplateView

from .forms import TimesheetEntryForm
from .models import TimesheetEntry


class ModuleHomeView(LoginRequiredMixin, TemplateView):
    template_name = "timesheets/home.html"

    def get_context_data(self, **kwargs):
        from datetime import date, timedelta
        ctx = super().get_context_data(**kwargs)
        company = getattr(self.request, "active_company", None)
        if company:
            qs = TimesheetEntry.objects.filter(company=company)
            today = date.today()
            week_start = today - timedelta(days=today.weekday())
            ctx["my_hours_this_week"] = sum(
                (e.hours for e in qs.filter(user=self.request.user, entry_date__gte=week_start)),
                Decimal(0),
            )
            ctx["total_hours_this_week"] = sum((e.hours for e in qs.filter(entry_date__gte=week_start)), Decimal(0))
            ctx["pending_count"] = qs.filter(status="SUBMITTED").count()
            ctx["recent_entries"] = qs.select_related("user", "project")[:12]
        return ctx


class EntryListView(LoginRequiredMixin, ListView):
    model = TimesheetEntry
    template_name = "timesheets/entry_list.html"
    context_object_name = "entries"
    paginate_by = 50

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return TimesheetEntry.objects.none()
        qs = TimesheetEntry.objects.filter(company=company).select_related("user", "project", "task")
        if self.request.GET.get("mine") == "1":
            qs = qs.filter(user=self.request.user)
        return qs


class EntryCreateView(LoginRequiredMixin, CreateView):
    model = TimesheetEntry
    form_class = TimesheetEntryForm
    template_name = "timesheets/entry_form.html"
    success_url = reverse_lazy("timesheets:entry_list")

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        form.instance.user = self.request.user
        messages.success(self.request, "Time entry added.")
        return super().form_valid(form)


class EntryDetailView(LoginRequiredMixin, DetailView):
    model = TimesheetEntry
    template_name = "timesheets/entry_detail.html"
    context_object_name = "entry"


@login_required
def entry_submit(request, pk):
    e = get_object_or_404(TimesheetEntry, pk=pk)
    if e.status == "DRAFT":
        e.status = "SUBMITTED"
        e.save(update_fields=["status"])
        messages.success(request, "Submitted for approval.")
    return redirect("timesheets:entry_detail", pk=pk)


@login_required
def entry_approve(request, pk):
    e = get_object_or_404(TimesheetEntry, pk=pk)
    if e.status == "SUBMITTED":
        e.status = "APPROVED"
        e.approved_by = request.user
        e.approved_at = timezone.now()
        e.save(update_fields=["status", "approved_by", "approved_at"])
        messages.success(request, "Approved.")
    return redirect("timesheets:entry_detail", pk=pk)
