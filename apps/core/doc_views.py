"""Shared views for printable + email-shareable business documents.

Every document (invoice, bill, PO, quote, receipt) renders through the
single `templates/_includes/document_shell.html` partial. This module
provides:

  * `DocumentPrintMixin` — pulls fields off a model instance into the
    shape the template expects.
  * `DocumentEmailView`  — sends the rendered HTML as an email with a
    short preview form.

Concrete views live next to each app (sales/urls.py, procurement/urls.py)
and just wire up `model` + a `_build_doc()` method.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.mail import EmailMultiAlternatives
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.views.generic import View


class DocumentPrintMixin(LoginRequiredMixin):
    """Mixin that renders a document through the shared document shell.

    Subclasses set `model`, `template_name="_includes/document_print.html"`,
    and implement `_build_doc(obj) -> dict` returning the context keys the
    shell expects (see the shell template's header comment).
    """
    model = None
    template_name = "_includes/document_print.html"

    def get(self, request, pk):
        obj = self.model.objects.get(pk=pk)
        ctx = self._build_doc(obj)
        ctx["doc_object"] = obj
        return render(request, self.template_name, ctx)

    def _build_doc(self, obj):  # pragma: no cover
        raise NotImplementedError


class DocumentEmailView(LoginRequiredMixin, View):
    """GET: a small compose form. POST: deliver the document by email.

    Subclasses set `model`, `print_url_name` and `build_doc(obj)` returning
    the same dict the printable shell takes.
    """
    model = None
    print_url_name = ""   # e.g. "sales:invoice_print"
    doc_label = "Document"

    def get(self, request, pk):
        obj = self.model.objects.get(pk=pk)
        default_to = (
            getattr(getattr(obj, "customer", None), "email", "") or
            getattr(getattr(obj, "supplier", None), "email", "") or ""
        )
        return render(request, "_includes/document_email_form.html", {
            "doc_object": obj,
            "doc_label": self.doc_label,
            "default_to": default_to,
            "default_subject": f"{self.doc_label} {getattr(obj, 'reference', '')}",
            "default_message": self._default_message(obj),
            "print_url_name": self.print_url_name,
        })

    def post(self, request, pk):
        obj = self.model.objects.get(pk=pk)
        to_raw = request.POST.get("to", "").strip()
        subject = request.POST.get("subject", "").strip() or f"{self.doc_label} {getattr(obj, 'reference', '')}"
        message = request.POST.get("message", "").strip()
        if not to_raw:
            messages.error(request, "Please enter at least one recipient.")
            return redirect(self.print_url_name, pk=pk)

        to_list = [addr.strip() for addr in to_raw.replace(";", ",").split(",") if addr.strip()]

        # Render the document body as the HTML email body.
        doc_ctx = self.build_doc(obj)
        doc_ctx["doc_object"] = obj
        doc_ctx["active_company"] = getattr(request, "active_company", None)
        html_body = render_to_string("_includes/document_email_body.html", doc_ctx, request=request)
        text_body = f"{message}\n\nSee attached {self.doc_label}: {getattr(obj, 'reference', '')}"

        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@merp.local")
        email = EmailMultiAlternatives(subject, text_body, from_email, to_list)
        email.attach_alternative(html_body, "text/html")
        try:
            email.send(fail_silently=False)
            messages.success(request, f"{self.doc_label} emailed to {', '.join(to_list)}.")
        except Exception as e:
            messages.error(request, f"Could not send email: {e}")
        return redirect(self.print_url_name, pk=pk)

    def build_doc(self, obj):  # pragma: no cover
        raise NotImplementedError

    def _default_message(self, obj):
        ref = getattr(obj, "reference", "")
        return (f"Dear Sir/Madam,\n\n"
                f"Please find attached {self.doc_label.lower()} {ref}.\n\n"
                f"Kind regards,\n"
                f"{getattr(self.request, 'active_company', None) or 'The Team'}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def lines_from_queryset(queryset, product_field="product"):
    """Normalise an invoice/order/PO/bill line queryset into the shape the
    document shell expects: [{description, sku, qty, unit_price, line_total}]."""
    out = []
    for line in queryset:
        product = getattr(line, product_field, None) if product_field else None
        qty = getattr(line, "quantity", None) or getattr(line, "qty", Decimal("0")) or Decimal("0")
        unit = getattr(line, "unit_price", None) or getattr(line, "price", Decimal("0")) or Decimal("0")
        total = getattr(line, "line_total", None)
        if total is None:
            total = (Decimal(qty) * Decimal(unit)).quantize(Decimal("0.01"))
        out.append({
            "description": getattr(line, "description", "") or (getattr(product, "name", "") if product else ""),
            "sku": getattr(product, "sku", "") if product else "",
            "qty": qty,
            "unit_price": unit,
            "line_total": total,
        })
    return out
