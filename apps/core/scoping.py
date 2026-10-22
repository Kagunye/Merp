"""Helpers that keep every query inside the active company.

Use these instead of hand-written `if company:` filters. When there is no
active company they return empty querysets, never every company's rows.
"""
from __future__ import annotations

from django.shortcuts import get_object_or_404


def company_of(request):
    return getattr(request, "active_company", None)


def scope(queryset, company, lookup: str = "company"):
    """Filter `queryset` to `company` through `lookup` (e.g. "employee__company")."""
    if company is None:
        return queryset.none()
    return queryset.filter(**{lookup: company})


def get_scoped_or_404(model_or_qs, request, lookup: str = "company", **kwargs):
    """get_object_or_404 that also requires the row to belong to the active company."""
    company = company_of(request)
    if company is None:
        from django.http import Http404
        raise Http404("No active company")
    return get_object_or_404(model_or_qs, **{lookup: company}, **kwargs)


def company_users(company):
    """Users who are active members of `company`."""
    from apps.accounts.models import User
    if company is None:
        return User.objects.none()
    return User.objects.filter(
        memberships__company=company, memberships__is_active=True, is_active=True
    ).distinct()


def narrow(form, company, **fields):
    """Restrict ForeignKey dropdowns on `form` to the active company.

    narrow(form, co, customer="company", task="project__company", sales_rep="users")
    The value is the lookup from the related model to Company, or "users" for
    a User field (restricted to the company's members).
    """
    for name, lookup in fields.items():
        if name not in form.fields:
            continue
        field = form.fields[name]
        if lookup == "users":
            field.queryset = company_users(company)
        else:
            field.queryset = scope(field.queryset.model._default_manager.all(), company, lookup)
    return form


class CompanyScopedMixin:
    """For class-based views with `model`: only rows of the active company.

    Set `company_lookup` when the link to Company is indirect, e.g.
    `company_lookup = "employee__company"`.
    """
    company_lookup = "company"

    def get_queryset(self):
        return scope(super().get_queryset(), company_of(self.request), self.company_lookup)
