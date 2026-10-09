import os

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView, DeleteView, DetailView, ListView, TemplateView,
)

from .forms import DocumentCategoryForm, DocumentForm
from .models import Document, DocumentCategory


class ModuleHomeView(LoginRequiredMixin, TemplateView):
    template_name = "documents/home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Documents"
        company = getattr(self.request, "active_company", None)
        if company:
            qs = Document.objects.filter(company=company)
            ctx["recent"] = qs[:12]
            ctx["doc_count"] = qs.count()
            ctx["categories"] = DocumentCategory.objects.filter(company=company)
        return ctx


class DocumentListView(LoginRequiredMixin, ListView):
    model = Document
    template_name = "documents/document_list.html"
    context_object_name = "documents"
    paginate_by = 24

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Document.objects.none()
        qs = Document.objects.filter(company=company).select_related("category", "uploaded_by")
        cat = self.request.GET.get("category")
        if cat:
            qs = qs.filter(category_id=cat)
        q = self.request.GET.get("q")
        if q:
            qs = qs.filter(name__icontains=q)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["categories"] = DocumentCategory.objects.filter(company=self.request.active_company)
        ctx["selected_category"] = self.request.GET.get("category") or ""
        ctx["search"] = self.request.GET.get("q", "")
        return ctx


class DocumentUploadView(LoginRequiredMixin, CreateView):
    model = Document
    form_class = DocumentForm
    template_name = "documents/document_form.html"
    success_url = reverse_lazy("documents:document_list")

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        form.instance.uploaded_by = self.request.user
        upload = form.cleaned_data.get("file")
        if upload:
            form.instance.file_size = upload.size
            form.instance.mime_type = getattr(upload, "content_type", "")
        messages.success(self.request, "Document uploaded.")
        return super().form_valid(form)


class DocumentDetailView(LoginRequiredMixin, DetailView):
    model = Document
    template_name = "documents/document_detail.html"
    context_object_name = "doc"


class DocumentDeleteView(LoginRequiredMixin, DeleteView):
    model = Document
    template_name = "documents/document_delete.html"
    success_url = reverse_lazy("documents:document_list")
    context_object_name = "doc"


class CategoryListView(LoginRequiredMixin, ListView):
    model = DocumentCategory
    template_name = "documents/category_list.html"
    context_object_name = "categories"

    def get_queryset(self):
        company = self.request.active_company
        return DocumentCategory.objects.filter(company=company) if company else DocumentCategory.objects.none()


class CategoryCreateView(LoginRequiredMixin, CreateView):
    model = DocumentCategory
    form_class = DocumentCategoryForm
    template_name = "documents/category_form.html"
    success_url = reverse_lazy("documents:category_list")

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        return super().form_valid(form)
