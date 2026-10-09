"""Executive dashboard views."""
import json
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.generic import TemplateView


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/index.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company

        if not company:
            ctx["page_title"] = "Dashboard"
            return ctx

        ctx["shortcuts"] = [
            ("/accounting/", "Accounting", "M9 7h6m0 10v-3m-3 3h.01M9 17h.01M9 14h.01M12 14h.01M15 11h.01M12 11h.01M9 11h.01M7 21h10a2 2 0 002-2V5a2 2 0 00-2-2H7a2 2 0 00-2 2v14a2 2 0 002 2z"),
            ("/banking/", "Banking", "M3 10h18M7 15h1m4 0h1m-7 4h12a3 3 0 003-3V8a3 3 0 00-3-3H6a3 3 0 00-3 3v8a3 3 0 003 3z"),
            ("/inventory/", "Inventory", "M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"),
            ("/crm/", "Customers", "M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M15 7a3 3 0 11-6 0 3 3 0 016 0z"),
            ("/procurement/", "Procurement", "M3 3h2l.4 2M7 13h10l4-8H5.4M7 13L5.4 5M7 13l-2.293 2.293c-.63.63-.184 1.707.707 1.707H17m0 0a2 2 0 100 4 2 2 0 000-4zm-8 2a2 2 0 11-4 0 2 2 0 014 0z"),
            ("/hr/", "HR", "M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z"),
            ("/payroll/", "Payroll", "M17 9V7a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2m2 4h10a2 2 0 002-2v-6a2 2 0 00-2-2H9a2 2 0 00-2 2v6a2 2 0 002 2zm7-5a2 2 0 11-4 0 2 2 0 014 0z"),
            ("/projects/", "Projects", "M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"),
            ("/reports/", "Reports", "M9 17v-2m3 2v-4m3 4v-6m2 10H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"),
        ]

        ctx.update(self._get_financial_summary(company))
        ctx.update(self._get_inventory_summary(company))
        ctx.update(self._get_hr_summary(company))
        ctx.update(self._get_recent_activity(company))
        return ctx

    def _get_financial_summary(self, company):
        today = date.today()
        month_start = today.replace(day=1)

        try:
            from apps.accounting.models import JournalEntryLine, Account
            # Cash/bank balances
            cash_accounts = Account.objects.filter(
                company=company,
                account_type__in=["CASH", "BANK"],
                is_active=True,
            )
            total_cash = sum(
                a.current_balance for a in cash_accounts
            )

            # Revenue this month
            revenue_accounts = Account.objects.filter(
                company=company,
                account_type="REVENUE",
                is_active=True,
            )
            monthly_revenue = JournalEntryLine.objects.filter(
                account__in=revenue_accounts,
                journal_entry__posting_date__gte=month_start,
                journal_entry__status="POSTED",
            ).aggregate(total=Sum("credit_amount"))["total"] or Decimal("0")

            # Expenses this month
            expense_accounts = Account.objects.filter(
                company=company,
                account_type="EXPENSE",
                is_active=True,
            )
            monthly_expenses = JournalEntryLine.objects.filter(
                account__in=expense_accounts,
                journal_entry__posting_date__gte=month_start,
                journal_entry__status="POSTED",
            ).aggregate(total=Sum("debit_amount"))["total"] or Decimal("0")

            # AR/AP
            from apps.sales.models import Invoice
            from apps.procurement.models import Bill
            accounts_receivable = Invoice.objects.filter(
                company=company,
                status__in=["SENT", "PARTIAL"],
            ).aggregate(total=Sum("outstanding_amount"))["total"] or Decimal("0")

            accounts_payable = Bill.objects.filter(
                company=company,
                status__in=["RECEIVED", "PARTIAL"],
            ).aggregate(total=Sum("outstanding_amount"))["total"] or Decimal("0")

        except Exception:
            total_cash = Decimal("0")
            monthly_revenue = Decimal("0")
            monthly_expenses = Decimal("0")
            accounts_receivable = Decimal("0")
            accounts_payable = Decimal("0")

        return {
            "total_cash": total_cash,
            "monthly_revenue": monthly_revenue,
            "monthly_expenses": monthly_expenses,
            "gross_profit": monthly_revenue - monthly_expenses,
            "accounts_receivable": accounts_receivable,
            "accounts_payable": accounts_payable,
        }

    def _get_inventory_summary(self, company):
        try:
            from apps.inventory.models import Product, StockLevel
            total_products = Product.objects.filter(company=company, is_active=True).count()
            low_stock = StockLevel.objects.filter(
                warehouse__company=company,
                quantity__lte=models.F("product__reorder_level"),
            ).count()
            inventory_value = StockLevel.objects.filter(
                warehouse__company=company,
            ).aggregate(
                total=Sum(models.ExpressionWrapper(
                    models.F("quantity") * models.F("product__cost_price"),
                    output_field=models.DecimalField(),
                ))
            )["total"] or Decimal("0")
        except Exception:
            total_products = 0
            low_stock = 0
            inventory_value = Decimal("0")

        return {
            "total_products": total_products,
            "low_stock_count": low_stock,
            "inventory_value": inventory_value,
        }

    def _get_hr_summary(self, company):
        try:
            from apps.hr.models import Employee
            headcount = Employee.objects.filter(
                company=company,
                status="ACTIVE",
            ).count()
        except Exception:
            headcount = 0

        return {"headcount": headcount}

    def _get_recent_activity(self, company):
        activities = []

        try:
            from apps.sales.models import Invoice
            recent_invoices = Invoice.objects.filter(
                company=company,
            ).select_related("customer").order_by("-created_at")[:5]
            for inv in recent_invoices:
                activities.append({
                    "type": "invoice",
                    "icon": "document-text",
                    "color": "green",
                    "label": f"Invoice {inv.reference}",
                    "detail": inv.customer.name if inv.customer else "—",
                    "amount": inv.total_amount,
                    "time": inv.created_at,
                })
        except Exception:
            pass

        try:
            from apps.procurement.models import PurchaseOrder
            recent_pos = PurchaseOrder.objects.filter(
                company=company,
            ).select_related("supplier").order_by("-created_at")[:3]
            for po in recent_pos:
                activities.append({
                    "type": "purchase_order",
                    "icon": "shopping-cart",
                    "color": "blue",
                    "label": f"PO {po.reference}",
                    "detail": po.supplier.name if po.supplier else "—",
                    "amount": po.total_amount,
                    "time": po.created_at,
                })
        except Exception:
            pass

        activities.sort(key=lambda x: x["time"], reverse=True)
        return {"recent_activities": activities[:8]}


@login_required
def switch_company(request, company_id):
    from apps.organizations.models import Company
    company = get_object_or_404(Company, id=company_id, is_active=True)
    if request.user.has_company_access(company):
        request.session["active_company_id"] = str(company.id)
        request.session.pop("active_branch_id", None)
    next_url = request.GET.get("next", "/dashboard/")
    return redirect(next_url)


@login_required
def switch_branch(request, branch_id):
    from apps.organizations.models import Branch
    branch = get_object_or_404(
        Branch, id=branch_id,
        company=request.active_company,
        is_active=True,
    )
    request.session["active_branch_id"] = str(branch.id)
    next_url = request.GET.get("next", "/dashboard/")
    return redirect(next_url)


@login_required
def kpi_widgets(request):
    """HTMX partial for KPI widgets."""
    company = request.active_company
    view = DashboardView()
    view.request = request
    data = {}
    if company:
        data.update(view._get_financial_summary(company))
        data.update(view._get_inventory_summary(company))
        data.update(view._get_hr_summary(company))
    from django.shortcuts import render
    return render(request, "dashboard/partials/kpi_widgets.html", data)


@login_required
def recent_transactions_widget(request):
    company = request.active_company
    activities = []
    if company:
        view = DashboardView()
        view.request = request
        data = view._get_recent_activity(company)
        activities = data.get("recent_activities", [])
    from django.shortcuts import render
    return render(request, "dashboard/partials/recent_transactions.html", {
        "recent_activities": activities
    })


@login_required
def sales_chart_widget(request):
    """Monthly sales data for Chart.js."""
    company = request.active_company
    today = date.today()
    months = []
    for i in range(11, -1, -1):
        d = (today.replace(day=1) - timedelta(days=i * 28)).replace(day=1)
        months.append(d)

    labels = [m.strftime("%b %Y") for m in months]
    revenue_data = []
    expense_data = []

    if company:
        for m in months:
            next_m = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
            try:
                from apps.accounting.models import JournalEntryLine, Account
                rev = JournalEntryLine.objects.filter(
                    account__company=company,
                    account__account_type="REVENUE",
                    journal_entry__posting_date__gte=m,
                    journal_entry__posting_date__lt=next_m,
                    journal_entry__status="POSTED",
                ).aggregate(total=Sum("credit_amount"))["total"] or 0
                exp = JournalEntryLine.objects.filter(
                    account__company=company,
                    account__account_type="EXPENSE",
                    journal_entry__posting_date__gte=m,
                    journal_entry__posting_date__lt=next_m,
                    journal_entry__status="POSTED",
                ).aggregate(total=Sum("debit_amount"))["total"] or 0
            except Exception:
                rev, exp = 0, 0
            revenue_data.append(float(rev))
            expense_data.append(float(exp))

    return JsonResponse({
        "labels": labels,
        "datasets": [
            {"label": "Revenue", "data": revenue_data, "color": "#C8102E"},
            {"label": "Expenses", "data": expense_data, "color": "#6B7280"},
        ],
    })


@login_required
def cashflow_chart_widget(request):
    company = request.active_company
    today = date.today()
    labels = []
    cash_data = []

    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        labels.append(d.strftime("%d %b"))
        try:
            from apps.accounting.models import Account
            cash_accounts = Account.objects.filter(
                company=company,
                account_type__in=["CASH", "BANK"],
                is_active=True,
            )
            balance = sum(float(a.current_balance) for a in cash_accounts)
        except Exception:
            balance = 0
        cash_data.append(balance)

    return JsonResponse({"labels": labels, "data": cash_data})
