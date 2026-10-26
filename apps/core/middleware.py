"""Core middleware for company context and request helpers."""
from django.utils.deprecation import MiddlewareMixin


class CompanyContextMiddleware(MiddlewareMixin):
    """Attach the user's active company to the request."""

    def process_request(self, request):
        request.active_company = None
        request.active_branch = None
        request.company_role = None

        if not request.user.is_authenticated:
            return

        # Get from session, fall back to user's default company
        company_id = request.session.get("active_company_id")
        if company_id:
            try:
                from apps.organizations.models import Company
                company = Company.objects.filter(
                    id=company_id, is_active=True
                ).first()
                if company and request.user.has_company_access(company):
                    request.active_company = company
            except Exception:
                pass

        # default_company hits the DB; a cold /tmp SQLite or an unmigrated
        # table would otherwise bubble up as a 500 here.
        if not request.active_company:
            try:
                default = request.user.default_company
            except Exception:
                default = None
            if default:
                request.active_company = default
                try:
                    request.session["active_company_id"] = str(default.id)
                except Exception:
                    pass

        # The user's role in the company they're working in: drives access.
        if request.active_company:
            try:
                request.company_role = request.user.get_company_role(request.active_company)
            except Exception:
                request.company_role = None

        # Active branch
        branch_id = request.session.get("active_branch_id")
        if branch_id and request.active_company:
            try:
                from apps.organizations.models import Branch
                branch = Branch.objects.filter(
                    id=branch_id, company=request.active_company, is_active=True
                ).first()
                request.active_branch = branch
            except Exception:
                pass


class RoleAccessMiddleware:
    """Enforce apps.core.access on every routed request.

    Runs after CompanyContextMiddleware, so `request.company_role` is the
    user's role in the company they are currently working in.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        from django.contrib.auth.views import redirect_to_login
        from django.http import HttpResponseForbidden
        from django.shortcuts import render

        from . import access

        match = request.resolver_match
        if match is None or match.namespace == "admin":
            return None
        ns, name = match.namespace or "", match.url_name or ""
        role = getattr(request, "company_role", None)
        if access.can(request.user, role, ns, name, request.method):
            return None
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if request.headers.get("HX-Request") or request.headers.get("Accept", "").startswith("application/json"):
            return HttpResponseForbidden("You don't have access to this.")
        context = {
            "module_label": access.MODULE_LABELS.get(ns, (ns or "this page").replace("_", " ").title()),
            "role_label": (role or "").replace("_", " ").title(),
            "is_write": request.method.upper() not in access.SAFE_METHODS,
            "no_company": not getattr(request, "active_company", None),
        }
        return render(request, "errors/403.html", context, status=403)
