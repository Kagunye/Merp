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


def can_delete(request, doc):
    """Deleting a company document is permanent, so it is for an admin or for
    the person who put it there -- not for anybody who can merely read the
    library. A contract, a KRA certificate or a signed delivery note removed by
    a read-only seat is evidence the company no longer has."""
    from apps.core.access import is_platform_admin
    if is_platform_admin(request.user) or getattr(request, "company_role", None) == "ADMIN":
        return True
    return doc.uploaded_by_id is not None and doc.uploaded_by_id == request.user.pk


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

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        from .models import DocumentCategory
        company = self.request.active_company
        form.fields["category"].queryset = (
            DocumentCategory.objects.filter(company=company) if company else DocumentCategory.objects.none())
        return form

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

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Document.objects.none()
        return Document.objects.filter(company=company)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["can_delete"] = can_delete(self.request, self.object)
        return ctx


class DocumentDeleteView(LoginRequiredMixin, DeleteView):
    model = Document
    template_name = "documents/document_delete.html"
    success_url = reverse_lazy("documents:document_list")
    context_object_name = "doc"

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return Document.objects.none()
        return Document.objects.filter(company=company)

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            doc = self.get_object()
            if not can_delete(request, doc):
                messages.error(request, "Only an admin or the person who uploaded this document "
                                        "can delete it. Ask one of them if it should go.")
                return redirect("documents:document_detail", pk=doc.pk)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        """Delete the row and the file it points at.

        Deleting only the row leaves the upload on disk (or in the bucket) with
        nothing pointing at it: it can no longer be found from the app, yet it
        is still stored, still paid for, and still readable by anyone with the
        path. The file goes after the row, and only once the row is gone, so a
        failed delete never leaves a record pointing at a missing file.
        """
        doc = self.object
        stored = doc.file.name if doc.file else ""
        storage = doc.file.storage if doc.file else None
        response = super().form_valid(form)
        if stored and storage is not None:
            try:
                storage.delete(stored)
            except Exception:
                # The record is gone either way; a file the storage cannot
                # remove now is a housekeeping job, not the user's error.
                pass
        messages.success(self.request, "Document deleted.")
        return response


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
