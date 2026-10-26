from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DetailView, ListView, TemplateView

from apps.core.mixins import CompanyRequiredMixin
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


class EntryCreateView(LoginRequiredMixin, CompanyRequiredMixin, CreateView):
    model = TimesheetEntry
    form_class = TimesheetEntryForm
    template_name = "timesheets/entry_form.html"
    success_url = reverse_lazy("timesheets:entry_list")

    def get_form_kwargs(self):
        # The form checks the day's total against this person's other entries,
        # so it has to know whose day it is before it validates.
        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.request.active_company
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        form.instance.user = self.request.user
        messages.success(self.request, "Time entry added.")
        return super().form_valid(form)


def company_entries(request):
    """Timesheet entries in the active company only — a pk from another book
    is a 404 here, not a page of somebody else's hours."""
    company = getattr(request, "active_company", None)
    if not company:
        return TimesheetEntry.objects.none()
    return TimesheetEntry.objects.filter(company=company)


class EntryDetailView(LoginRequiredMixin, DetailView):
    model = TimesheetEntry
    template_name = "timesheets/entry_detail.html"
    context_object_name = "entry"

    def get_queryset(self):
        return company_entries(self.request)


@login_required
def entry_submit(request, pk):
    e = get_object_or_404(company_entries(request), pk=pk)
    if e.status == "DRAFT":
        e.status = "SUBMITTED"
        e.save(update_fields=["status"])
        messages.success(request, "Submitted for approval.")
    return redirect("timesheets:entry_detail", pk=pk)


@login_required
def entry_approve(request, pk):
    e = get_object_or_404(company_entries(request), pk=pk)
    if e.user_id == request.user.pk:
        # Hours you approve for yourself are hours nobody else agreed you worked.
        messages.error(request, "You cannot approve your own hours — somebody else has to.")
        return redirect("timesheets:entry_detail", pk=pk)
    if e.status == "SUBMITTED":
        e.status = "APPROVED"
        e.approved_by = request.user
        e.approved_at = timezone.now()
        e.save(update_fields=["status", "approved_by", "approved_at"])
        messages.success(request, "Approved.")
    return redirect("timesheets:entry_detail", pk=pk)

