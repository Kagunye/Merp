from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, TemplateView, UpdateView

from .forms import CandidateForm, InterviewForm, JobOpeningForm
from .models import Candidate, Interview, JobOpening


class ModuleHomeView(LoginRequiredMixin, TemplateView):
    template_name = "recruitment/home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = getattr(self.request, "active_company", None)
        if company:
            openings = JobOpening.objects.filter(company=company)
            ctx["open_openings"] = openings.filter(status="OPEN").count()
            ctx["all_openings"] = openings.count()
            ctx["candidates_active"] = Candidate.objects.filter(
                opening__company=company,
                stage__in=["APPLIED", "SCREENING", "INTERVIEW", "OFFER"],
            ).count()
            ctx["interviews_upcoming"] = Interview.objects.filter(
                candidate__opening__company=company, status="SCHEDULED",
            ).count()
            ctx["recent_openings"] = openings[:8]
            ctx["recent_candidates"] = Candidate.objects.filter(
                opening__company=company,
            ).select_related("opening")[:8]
        return ctx


# ---- Job openings ----
class OpeningListView(LoginRequiredMixin, ListView):
    model = JobOpening
    template_name = "recruitment/opening_list.html"
    context_object_name = "openings"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return JobOpening.objects.none()
        return JobOpening.objects.filter(company=company)


class OpeningCreateView(LoginRequiredMixin, CreateView):
    model = JobOpening
    form_class = JobOpeningForm
    template_name = "recruitment/opening_form.html"

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("recruitment:opening_detail", args=[self.object.pk])


class OpeningUpdateView(LoginRequiredMixin, UpdateView):
    model = JobOpening
    form_class = JobOpeningForm
    template_name = "recruitment/opening_form.html"

    def get_success_url(self):
        return reverse("recruitment:opening_detail", args=[self.object.pk])


class OpeningDetailView(LoginRequiredMixin, DetailView):
    model = JobOpening
    template_name = "recruitment/opening_detail.html"
    context_object_name = "opening"


# ---- Candidates ----
class CandidateListView(LoginRequiredMixin, ListView):
    model = Candidate
    template_name = "recruitment/candidate_list.html"
    context_object_name = "candidates"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Candidate.objects.none()
        return Candidate.objects.filter(opening__company=company).select_related("opening")


class CandidateCreateView(LoginRequiredMixin, CreateView):
    model = Candidate
    form_class = CandidateForm
    template_name = "recruitment/candidate_form.html"
    success_url = reverse_lazy("recruitment:candidate_list")

    def form_valid(self, form):
        messages.success(self.request, "Candidate added.")
        return super().form_valid(form)


class CandidateDetailView(LoginRequiredMixin, DetailView):
    model = Candidate
    template_name = "recruitment/candidate_detail.html"
    context_object_name = "candidate"


# ---- Interviews ----
class InterviewListView(LoginRequiredMixin, ListView):
    model = Interview
    template_name = "recruitment/interview_list.html"
    context_object_name = "interviews"

    def get_queryset(self):
        company = self.request.active_company
        return Interview.objects.filter(candidate__opening__company=company).select_related("candidate", "interviewer") if company else Interview.objects.none()


class InterviewCreateView(LoginRequiredMixin, CreateView):
    model = Interview
    form_class = InterviewForm
    template_name = "recruitment/interview_form.html"
    success_url = reverse_lazy("recruitment:interview_list")
