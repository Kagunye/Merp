"""Projects views."""
import datetime
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import CreateView, DetailView, ListView, TemplateView

from apps.accounts.models import User
from .models import Project, Task
from apps.core.forms import ISO_DATE


class ProjectListView(LoginRequiredMixin, ListView):
    template_name = "projects/project_list.html"
    context_object_name = "projects"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Project.objects.none()
        qs = Project.objects.filter(company=company, is_active=True).select_related("customer", "manager")
        status = self.request.GET.get("status")
        if status:
            qs = qs.filter(status=status)
        q = self.request.GET.get("q")
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(reference__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Projects"
        ctx["status_choices"] = Project.STATUS_CHOICES
        ctx["status_filter"] = self.request.GET.get("status", "")
        ctx["q"] = self.request.GET.get("q", "")
        return ctx


class ProjectCreateView(LoginRequiredMixin, CreateView):
    model = Project
    template_name = "projects/project_form.html"
    fields = ["reference", "name", "description", "status", "start_date", "end_date", "budget", "customer", "manager", "branch", "notes"]

    def get_success_url(self):
        from django.urls import reverse
        return reverse("projects:project_detail", kwargs={"pk": self.object.pk})

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        company = self.request.active_company
        from django import forms as dforms
        for fname, field in form.fields.items():
            if isinstance(field.widget, dforms.Select):
                field.widget.attrs["class"] = "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
            elif isinstance(field.widget, dforms.Textarea):
                field.widget.attrs["class"] = "form-textarea w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
                field.widget.attrs["rows"] = 3
            elif isinstance(field.widget, dforms.DateInput):
                field.widget.attrs["class"] = "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
                field.widget.attrs["type"] = "date"
                field.widget.format = ISO_DATE
            else:
                field.widget.attrs["class"] = "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
        if company:
            from apps.crm.models import Customer
            from apps.organizations.models import Branch
            form.fields["customer"].queryset = Customer.objects.filter(company=company, is_active=True)
            form.fields["branch"].queryset = Branch.objects.filter(company=company, is_active=True)
            from apps.core.models import SequenceCounter
            # A preview: the number is taken at save time (claim_number), so
            # opening this form and leaving does not leave a gap in the series.
            form.fields["reference"].initial = SequenceCounter.peek_number(company, "PRJ-")
        from apps.core.forms import company_users
        form.fields["manager"].queryset = company_users(company)
        form.fields["start_date"].initial = datetime.date.today()
        return form

    def form_valid(self, form):
        from apps.core.models import SequenceCounter
        company = self.request.active_company
        form.instance.company = company
        form.instance.reference = SequenceCounter.claim_number(
            company, "PRJ-", form.instance.reference,
            taken=lambda ref: Project.objects.filter(company=company, reference=ref).exists())
        form.instance.manager = self.request.user
        messages.success(self.request, f"Project '{form.instance.name}' created.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "New Project"
        return ctx


class ProjectDetailView(LoginRequiredMixin, DetailView):
    model = Project
    template_name = "projects/project_detail.html"
    context_object_name = "project"

    def get_queryset(self):
        return Project.objects.filter(company=self.request.active_company)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = self.object.name
        ctx["tasks"] = self.object.tasks.select_related("assigned_to").order_by("status", "due_date")
        ctx["task_counts"] = {
            "total": self.object.tasks.count(),
            "done": self.object.tasks.filter(status="DONE").count(),
            "in_progress": self.object.tasks.filter(status="IN_PROGRESS").count(),
            "todo": self.object.tasks.filter(status="TODO").count(),
        }
        return ctx


class TaskCreateView(LoginRequiredMixin, CreateView):
    model = Task
    template_name = "projects/task_form.html"
    fields = ["title", "description", "status", "priority", "assigned_to", "due_date", "estimated_hours", "notes"]

    @property
    def project(self):
        """The project fixed by the URL, or None when creating a task standalone."""
        return get_object_or_404(
            Project, pk=self.kwargs["project_pk"], company=self.request.active_company
        )

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        from django import forms as dforms
        company = self.request.active_company
        if "project_pk" not in self.kwargs:
            form.fields["project"] = dforms.ModelChoiceField(
                label="Project",
                queryset=Project.objects.filter(company=company, is_active=True).order_by("name"),
                required=True,
            )
        if "assigned_to" in form.fields:
            from apps.core.forms import company_users
            form.fields["assigned_to"].queryset = company_users(company)
        for fname, field in form.fields.items():
            if isinstance(field.widget, dforms.Select):
                field.widget.attrs["class"] = "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
            elif isinstance(field.widget, dforms.Textarea):
                field.widget.attrs["class"] = "form-textarea w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
                field.widget.attrs["rows"] = 3
            elif isinstance(field.widget, dforms.DateInput):
                field.widget.attrs["class"] = "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
                field.widget.attrs["type"] = "date"
                field.widget.format = ISO_DATE
            else:
                field.widget.attrs["class"] = "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
        # Keep "project" first in the visual order when it is present.
        if "project" in form.fields:
            order = ["project", "title", "description", "status", "priority",
                     "assigned_to", "due_date", "estimated_hours", "notes"]
            form.fields = {k: form.fields[k] for k in order if k in form.fields}
        return form

    def get_success_url(self):
        from django.urls import reverse
        if "project_pk" in self.kwargs:
            return reverse("projects:project_detail", kwargs={"pk": self.kwargs["project_pk"]})
        return reverse("projects:task_detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        if "project_pk" in self.kwargs:
            form.instance.project = self.project
        messages.success(self.request, f"Task '{form.instance.title}' added.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["project"] = self.project if "project_pk" in self.kwargs else None
        ctx["page_title"] = "New Task"
        return ctx


class TaskDetailView(LoginRequiredMixin, DetailView):
    model = Task
    template_name = "projects/task_detail.html"
    context_object_name = "task"

    def get_queryset(self):
        return Task.objects.filter(project__company=self.request.active_company)


class TaskListView(LoginRequiredMixin, ListView):
    template_name = "projects/task_list.html"
    context_object_name = "tasks"
    paginate_by = 50

    def get_queryset(self):
        from apps.projects.models import Task
        company = self.request.active_company
        qs = Task.objects.filter(project__company=company) if company else Task.objects.none()
        return qs.select_related("project", "assigned_to")


class ProjectsIndexView(LoginRequiredMixin, TemplateView):
    template_name = "projects/index.html"

    def get_context_data(self, **kwargs):
        from datetime import date, timedelta
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if not company:
            return ctx
        projs = Project.objects.filter(company=company)
        ctx["active_projects"] = projs.filter(status__in=["PLANNING", "ACTIVE"]).count()
        ctx["completed_projects"] = projs.filter(status="COMPLETED").count()
        tasks = Task.objects.filter(project__company=company)
        ctx["open_tasks"] = tasks.exclude(status__in=["DONE", "CANCELLED"]).count()
        today = date.today()
        ctx["due_this_week"] = tasks.filter(
            due_date__gte=today, due_date__lte=today + timedelta(days=7),
        ).exclude(status__in=["DONE", "CANCELLED"]).count()
        ctx["recent_projects"] = projs.order_by("-created_at")[:6]
        ctx["recent_tasks"] = tasks.select_related("project").order_by("-created_at")[:6]
        return ctx
