from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DetailView, ListView, TemplateView

from .forms import CloseSessionForm, OpenSessionForm
from .models import POSSale, POSSession


class ModuleHomeView(LoginRequiredMixin, TemplateView):
    template_name = "pos/home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Point of Sale"
        company = getattr(self.request, "active_company", None)
        if company:
            ctx["open_session"] = POSSession.objects.filter(
                company=company, cashier=self.request.user, status="OPEN"
            ).first()
            ctx["recent_sessions"] = POSSession.objects.filter(company=company)[:10]
        return ctx


class SessionListView(LoginRequiredMixin, ListView):
    model = POSSession
    template_name = "pos/session_list.html"
    context_object_name = "sessions"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return POSSession.objects.none()
        return POSSession.objects.filter(company=company).select_related("cashier", "branch")


class SessionOpenView(LoginRequiredMixin, CreateView):
    model = POSSession
    form_class = OpenSessionForm
    template_name = "pos/session_open.html"
    success_url = reverse_lazy("pos:home")

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        form.instance.cashier = self.request.user
        form.instance.opened_at = timezone.now()
        form.instance.status = "OPEN"
        messages.success(self.request, "POS session opened.")
        return super().form_valid(form)


@login_required
def session_close(request, pk):
    session = get_object_or_404(POSSession, pk=pk)
    if request.method == "POST":
        form = CloseSessionForm(request.POST, instance=session)
        if form.is_valid():
            session = form.save(commit=False)
            session.closed_at = timezone.now()
            session.status = "CLOSED"
            session.save()
            messages.success(request, "Session closed.")
            return redirect("pos:session_detail", pk=session.pk)
    else:
        form = CloseSessionForm(instance=session)
    return render(request, "pos/session_close.html", {"form": form, "session": session})


class SessionDetailView(LoginRequiredMixin, DetailView):
    model = POSSession
    template_name = "pos/session_detail.html"
    context_object_name = "session"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["sales"] = self.object.sales.all()[:50]
        return ctx


class SaleListView(LoginRequiredMixin, ListView):
    model = POSSale
    template_name = "pos/sale_list.html"
    context_object_name = "sales"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return POSSale.objects.none()
        return POSSale.objects.filter(session__company=company).select_related("session", "customer")


class SaleDetailView(LoginRequiredMixin, DetailView):
    model = POSSale
    template_name = "pos/sale_detail.html"
    context_object_name = "sale"


class TerminalView(LoginRequiredMixin, TemplateView):
    """Simple cashier terminal view. Full product-grid interactions
    belong in a dedicated SPA; this is the stub."""
    template_name = "pos/terminal.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        ctx["session"] = POSSession.objects.filter(
            company=company, cashier=self.request.user, status="OPEN"
        ).first() if company else None
        return ctx
