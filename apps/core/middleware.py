"""Core middleware for company context and request helpers."""
from django.utils.deprecation import MiddlewareMixin


class CompanyContextMiddleware(MiddlewareMixin):
    """Attach the user's active company to the request."""

    def process_request(self, request):
        request.active_company = None
        request.active_branch = None

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

        if not request.active_company and request.user.default_company:
            request.active_company = request.user.default_company
            request.session["active_company_id"] = str(request.active_company.id)

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
