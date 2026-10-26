from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DetailView, ListView, TemplateView

from apps.core.mixins import CompanyRequiredMixin
from apps.hr.access import sees_all_hours
from .forms import TimesheetEntryForm
from .models import TimesheetEntry


def company_entries(request):
    """The timesheet entries this login may see, in the active company only.

    A pk from another book is a 404 here, not a page of somebody else's hours.
    Inside the book, HR, administrators and project managers see everybody's
    time — approving it is their job — and everybody else sees their own and
    nothing more.
    """
    company = getattr(request, "active_company", None)
    if not company:
        return TimesheetEntry.objects.none()
    qs = TimesheetEntry.objects.filter(company=company)
    if sees_all_hours(request.user, company):
        return qs
    return qs.filter(user=request.user)


class ModuleHomeView(LoginRequiredMixin, TemplateView):
    template_name = "timesheets/home.html"

    def get_context_data(self, **kwargs):
        from datetime import date, timedelta
        ctx = super().get_context_data(**kwargs)
        company = getattr(self.request, "active_company", None)
        if company:
            # The week's totals and the recent list are the same privacy as
            # the entry list: a member of staff's "total" is their own hours.
            qs = company_entries(self.request)
            today = date.today()
            week_start = today - timedelta(days=today.weekday())
            ctx["my_hours_this_week"] = sum(
                (e.hours for e in qs.filter(user=self.request.user, entry_date__gte=week_start)),
                Decimal(0),
            )
            ctx["total_hours_this_week"] = sum((e.hours for e in qs.filter(entry_date__gte=week_start)), Decimal(0))
            ctx["pending_count"] = qs.filter(status="SUBMITTED").count()
            ctx["recent_entries"] = qs.select_related("user", "project")[:12]
            ctx["sees_all"] = sees_all_hours(self.request.user, company)
        return ctx


class EntryListView(LoginRequiredMixin, ListView):
    model = TimesheetEntry
    template_name = "timesheets/entry_list.html"
    context_object_name = "entries"
    paginate_by = 50

    def get_queryset(self):
        qs = company_entries(self.request).select_related("user", "project", "task")
        if self.request.GET.get("mine") == "1":
            qs = qs.filter(user=self.request.user)
        status = self.request.GET.get("status")
        if status in dict(TimesheetEntry.STATUS_CHOICES):
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = getattr(self.request, "active_company", None)
        ctx["subtitle"] = ("All time logged in the organisation"
                           if sees_all_hours(self.request.user, company)
                           else "The time you have logged")
        return ctx


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


class EntryDetailView(LoginRequiredMixin, DetailView):
    model = TimesheetEntry
    template_name = "timesheets/entry_detail.html"
    context_object_name = "entry"

    def get_queryset(self):
        return company_entries(self.request)


@login_required
@require_POST
def entry_submit(request, pk):
    e = get_object_or_404(company_entries(request), pk=pk)
    if e.user_id != request.user.pk:
        messages.error(request, "Only the person who logged these hours can submit them.")
    elif e.status != "DRAFT":
        messages.error(request, "This entry has already been submitted.")
    else:
        e.status = "SUBMITTED"
        e.save(update_fields=["status"])
        messages.success(request, "Submitted for approval.")
    return redirect("timesheets:entry_detail", pk=pk)


@login_required
@require_POST
def entry_approve(request, pk):
    e = get_object_or_404(company_entries(request), pk=pk)
    if e.user_id == request.user.pk:
        # Hours you approve for yourself are hours nobody else agreed you worked.
        messages.error(request, "You cannot approve your own hours — somebody else has to.")
    elif e.status != "SUBMITTED":
        messages.error(request, "Only submitted hours can be approved.")
    else:
        e.status = "APPROVED"
        e.approved_by = request.user
        e.approved_at = timezone.now()
        e.save(update_fields=["status", "approved_by", "approved_at"])
        messages.success(request, "Approved.")
    return redirect("timesheets:entry_detail", pk=pk)

