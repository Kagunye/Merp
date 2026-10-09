from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DetailView, ListView, TemplateView

from .forms import TicketCommentForm, TicketForm
from .models import Ticket


class ModuleHomeView(LoginRequiredMixin, TemplateView):
    template_name = "helpdesk/home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = getattr(self.request, "active_company", None)
        if company:
            qs = Ticket.objects.filter(company=company)
            ctx["open_count"] = qs.filter(status__in=["OPEN", "IN_PROGRESS"]).count()
            ctx["waiting_count"] = qs.filter(status="WAITING").count()
            ctx["resolved_count"] = qs.filter(status="RESOLVED").count()
            ctx["urgent_count"] = qs.filter(priority="URGENT", status__in=["OPEN", "IN_PROGRESS"]).count()
            ctx["my_tickets"] = qs.filter(assigned_to=self.request.user, status__in=["OPEN", "IN_PROGRESS"])[:10]
            ctx["recent"] = qs.select_related("customer", "assigned_to")[:10]
        return ctx


class TicketListView(LoginRequiredMixin, ListView):
    model = Ticket
    template_name = "helpdesk/ticket_list.html"
    context_object_name = "tickets"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Ticket.objects.none()
        qs = Ticket.objects.filter(company=company).select_related("customer", "assigned_to")
        status = self.request.GET.get("status")
        if status: qs = qs.filter(status=status)
        return qs


class TicketCreateView(LoginRequiredMixin, CreateView):
    model = Ticket
    form_class = TicketForm
    template_name = "helpdesk/ticket_form.html"

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        messages.success(self.request, "Ticket opened.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("helpdesk:ticket_detail", args=[self.object.pk])


class TicketDetailView(LoginRequiredMixin, DetailView):
    model = Ticket
    template_name = "helpdesk/ticket_detail.html"
    context_object_name = "ticket"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["comment_form"] = TicketCommentForm()
        return ctx


@login_required
def ticket_comment(request, pk):
    ticket = get_object_or_404(Ticket, pk=pk)
    form = TicketCommentForm(request.POST)
    if form.is_valid():
        c = form.save(commit=False)
        c.ticket = ticket
        c.author = request.user
        c.save()
        messages.success(request, "Reply posted.")
    return redirect("helpdesk:ticket_detail", pk=pk)


@login_required
def ticket_resolve(request, pk):
    t = get_object_or_404(Ticket, pk=pk)
    t.status = "RESOLVED"
    t.resolved_at = timezone.now()
    t.save()
    messages.success(request, "Ticket resolved.")
    return redirect("helpdesk:ticket_detail", pk=pk)


@login_required
def ticket_close(request, pk):
    t = get_object_or_404(Ticket, pk=pk)
    t.status = "CLOSED"
    t.closed_at = timezone.now()
    t.save()
    messages.success(request, "Ticket closed.")
    return redirect("helpdesk:ticket_detail", pk=pk)
