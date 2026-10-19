"""Shared view mixins."""
from django.contrib import messages
from django.shortcuts import redirect


class CompanyRequiredMixin:
    """Views that stamp ``request.active_company`` onto saved rows cannot work
    without one — the insert would fail on the NOT NULL company column."""

    def dispatch(self, request, *args, **kwargs):
        if not getattr(request, "active_company", None):
            messages.error(request, "Create or switch to a company before adding records.")
            return redirect("organizations:company_create")
        return super().dispatch(request, *args, **kwargs)
