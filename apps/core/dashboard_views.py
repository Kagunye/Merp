"""Executive dashboard views."""
import json
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import (
    Count, DecimalField, ExpressionWrapper, F, Sum,
)
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.generic import TemplateView


def page_rights(request):
    """`may("sales:invoice_list")` -> may this login open that page?

    The dashboard and the search box gather figures from every module onto one
    page, which is exactly how a role ends up reading what its own menu would
    never show it: a till cashier seeing this month's revenue, a sales rep the
    payroll headcount, a viewer the journal references. So every widget and
    every result type asks the same role table the middleware enforces, for the
    page it summarises or links to, and is left out when the answer is no.
    `may(target, "POST")` asks whether the role may act there, for the
    shortcuts that open a form and the approval queues.
    """
    from django.urls import resolve, Resolver404
    from . import access
    role = getattr(request, "company_role", None)

    def may(target, method="GET"):
        if target.startswith("/"):
            try:
                match = resolve(target)
            except Resolver404:
                return False
            ns, name = match.namespace, match.url_name
        else:
            ns, _, name = target.rpartition(":")
        return access.can(request.user, role, ns, name, method)

    return may


# Each dashboard widget, and the page whose readers may see it.
DASHBOARD_WIDGETS = {
    "cash": "banking:account_list",
    "receivables": "sales:ar_aging",
    "payables": "procurement:ap_aging",
    "trading": "reporting:profit_loss",
    "open_invoices": "sales:invoice_list",
    "open_bills": "procurement:bill_list",
    "inventory": "inventory:product_list",
    "sales_orders": "sales:order_list",
    "purchase_orders": "procurement:po_list",
    "journals": "accounting:journal_entry_list",
}

# Each approval queue, and the action whose holders work it. A queue is a
# count of other people's requests waiting on a decision; it is shown to the
# people who make that decision, not to everybody who can file one.
APPROVAL_QUEUES = {
    "requisitions": "procurement:requisition_approve",
    "expense_claims": "expenses:claim_approve",
    "leave": "hr:leave_approve",
    "timesheets": "timesheets:entry_approve",
}

# The quick-action buttons: each opens a form, so each is shown only to a role
# that may both open it and save it.
QUICK_ACTIONS = [
    ("sales:invoice_create", "New Invoice"),
    ("sales:payment_create", "Record Receipt"),
    ("procurement:bill_create", "New Bill"),
    ("accounting:journal_entry_create", "Journal Entry"),
    ("crm:customer_create", "New Customer"),
    ("inventory:product_create", "New Product"),
]


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/index.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company

        if not company:
            ctx["page_title"] = "Dashboard"
            return ctx

        from apps.accounting.currency import book_currency
        from django.urls import reverse
        ctx["book"] = book_currency(company)
        may = page_rights(self.request)
        show = {key: may(target) for key, target in DASHBOARD_WIDGETS.items()}
        show.update({key: may(target, "POST") for key, target in APPROVAL_QUEUES.items()})
        show["approvals"] = any(show[key] for key in APPROVAL_QUEUES)
        ctx["show"] = show
        ctx["quick_actions"] = [
            (reverse(name), label) for name, label in QUICK_ACTIONS
            if may(name) and may(name, "POST")]

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
        ctx["shortcuts"] = [s for s in ctx["shortcuts"] if may(s[0])]

        # Only the figures this role may read are worked out at all, so none
        # of them can surface through the context by another route.
        if show["cash"] or show["receivables"] or show["payables"] or show["trading"]:
            summary = self._get_financial_summary(company)
            hidden = {"cash": ["total_cash"], "receivables": ["accounts_receivable"],
                      "payables": ["accounts_payable"],
                      "trading": ["monthly_revenue", "monthly_expenses", "gross_profit"]}
            for key, fields in hidden.items():
                if not show[key]:
                    for field in fields:
                        summary.pop(field, None)
            ctx.update(summary)
        if show["inventory"]:
            ctx.update(self._get_inventory_summary(company))
        if may("hr:employee_list"):
            ctx.update(self._get_hr_summary(company))
        ctx.update(self._get_recent_activity(company, may))
        ctx.update(self._get_dashboard_widgets(company, show))
        if show["trading"]:
            ctx["net_profit_mtd"] = ctx.get("monthly_revenue", 0) - ctx.get("monthly_expenses", 0)
        return ctx

    def _get_dashboard_widgets(self, company, show=None):
        """Extra lists / counts the dashboard surface needs.

        `show` says which widgets this role may see (see DASHBOARD_WIDGETS);
        anything it may not see is left at its empty default and never queried.
        """
        from datetime import date
        from decimal import Decimal
        show = show if show is not None else {
            key: True for key in list(DASHBOARD_WIDGETS) + list(APPROVAL_QUEUES)}
        out = {
            "net_profit_mtd": Decimal("0"),
            "open_invoices": [],
            "open_bills": [],
            "open_sales_orders": 0,
            "open_purchase_orders": 0,
            "pending_requisitions": 0,
            "pending_expense_claims": 0,
            "pending_leave_requests": 0,
            "pending_timesheets": 0,
            "recent_journal_entries": [],
        }
        try:
            from apps.sales.models import Invoice, SalesOrder
            if show["open_invoices"]:
                out["open_invoices"] = list(
                    Invoice.objects.filter(
                        company=company,
                        status__in=["SENT", "PARTIAL", "OVERDUE"],
                    ).select_related("customer").order_by("due_date")[:6]
                )
            if show["sales_orders"]:
                out["open_sales_orders"] = SalesOrder.objects.filter(
                    company=company,
                    status__in=["DRAFT", "CONFIRMED", "PARTIALLY_DELIVERED"],
                ).count()
        except Exception:
            pass

        try:
            from apps.procurement.models import Bill, PurchaseOrder, PurchaseRequisition
            if show["open_bills"]:
                out["open_bills"] = list(
                    Bill.objects.filter(
                        company=company,
                        status__in=["RECEIVED", "PARTIAL", "OVERDUE"],
                    ).select_related("supplier").order_by("due_date")[:6]
                )
            if show["purchase_orders"]:
                out["open_purchase_orders"] = PurchaseOrder.objects.filter(
                    company=company,
                    status__in=["DRAFT", "SENT", "PARTIALLY_RECEIVED"],
                ).count()
            if show["requisitions"]:
                out["pending_requisitions"] = PurchaseRequisition.objects.filter(
                    company=company, status="PENDING_APPROVAL",
                ).count()
        except Exception:
            pass

        try:
            from apps.expenses.models import ExpenseClaim
            if show["expense_claims"]:
                out["pending_expense_claims"] = ExpenseClaim.objects.filter(
                    company=company, status="SUBMITTED",
                ).count()
        except Exception:
            pass

        try:
            from apps.hr.models import LeaveRequest
            if show["leave"]:
                out["pending_leave_requests"] = LeaveRequest.objects.filter(
                    company=company, status="PENDING",
                ).count()
        except Exception:
            pass

        try:
            from apps.timesheets.models import TimesheetEntry
            if show["timesheets"]:
                out["pending_timesheets"] = TimesheetEntry.objects.filter(
                    company=company, status="SUBMITTED",
                ).count()
        except Exception:
            pass

        try:
            from apps.accounting.models import JournalEntry
            if show["journals"]:
                out["recent_journal_entries"] = list(
                    JournalEntry.objects.filter(
                        company=company, status="POSTED",
                        # Recent activity is what has happened: an entry dated
                        # next month is not part of it yet.
                        posting_date__lte=date.today(),
                    ).order_by("-posting_date", "-created_at")[:6]
                )
        except Exception:
            pass

        # net_profit_mtd: computed from the financial summary already in context,
        # so fall back to 0 and let the view-level computation set it below.
        return out

    def _get_financial_summary(self, company):
        today = date.today()
        month_start = today.replace(day=1)

        try:
            from apps.accounting.models import JournalEntryLine, Account
            # Cash/bank balances as at today: a receipt dated next week is not
            # money in the bank yet, however surely it is expected. Retired
            # accounts are kept in: one cannot be retired while it holds money,
            # so it adds nothing today, but it must not drop out if it did.
            cash_accounts = Account.objects.filter(
                company=company,
                account_type__in=["CASH", "BANK"],
            )
            total_cash = sum(
                (a.balance_as_at(today) for a in cash_accounts), Decimal("0")
            )

            # Revenue and expenses month to date, netted exactly as the P&L
            # nets them: credits less debits for revenue, debits less credits
            # for costs, closing entries excluded, and no postings after today
            # (a future-dated reversal is not this month's trading).
            month_lines = JournalEntryLine.objects.filter(
                journal_entry__company=company,
                journal_entry__posting_date__gte=month_start,
                journal_entry__posting_date__lte=today,
                journal_entry__status="POSTED",
            ).exclude(journal_entry__entry_type="CLOSING")

            def _net(account_types):
                agg = month_lines.filter(
                    account__account_type__in=account_types,
                ).aggregate(dr=Sum("debit_amount"), cr=Sum("credit_amount"))
                return (agg["dr"] or Decimal("0")), (agg["cr"] or Decimal("0"))

            rev_dr, rev_cr = _net(["REVENUE"])
            monthly_revenue = rev_cr - rev_dr

            cost_dr, cost_cr = _net(["EXPENSE", "COST_OF_GOODS"])
            monthly_expenses = cost_dr - cost_cr

            # AR/AP
            from apps.sales.models import Invoice
            from apps.procurement.models import Bill
            # Added in the book's money: the papers may be in several currencies,
            # and the receivable is what the ledger carries, not the sum of the
            # figures printed on them.
            # Net of money customers paid that no invoice has taken yet: that
            # cash sits in AR as a credit, so the open invoices alone overstate
            # the receivable the ledger carries (AR 1300) by exactly that much.
            from apps.sales.receivables import net_receivable
            accounts_receivable = net_receivable(company)
            accounts_payable = sum(
                (b.outstanding_base for b in Bill.objects.filter(
                    company=company, status__in=["RECEIVED", "PARTIAL", "OVERDUE"],
                    outstanding_amount__gt=Decimal("0"))), Decimal("0"))

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
                quantity__lte=F("product__reorder_level"),
            ).count()
            inventory_value = StockLevel.objects.filter(
                warehouse__company=company,
            ).aggregate(
                total=Sum(ExpressionWrapper(
                    F("quantity") * F("product__cost_price"),
                    output_field=DecimalField(),
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

    def _get_recent_activity(self, company, may=None):
        """The latest invoices and purchase orders, each only for a role that
        may open that list (`may`, from page_rights; None means no limit)."""
        activities = []
        may = may or (lambda target, method="GET": True)

        if may("sales:invoice_list"):
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

        if may("procurement:po_list"):
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
    from apps.accounts.views import safe_next
    return redirect(safe_next(request, "/dashboard/"))


@login_required
def switch_branch(request, branch_id):
    from apps.organizations.models import Branch
    branch = get_object_or_404(
        Branch, id=branch_id,
        company=request.active_company,
        is_active=True,
    )
    request.session["active_branch_id"] = str(branch.id)
    from apps.accounts.views import safe_next
    return redirect(safe_next(request, "/dashboard/"))


@login_required
def kpi_widgets(request):
    """HTMX partial for KPI widgets (finance and audit only, by the role table)."""
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
        data = view._get_recent_activity(company, page_rights(request))
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
                # Netted the way the P&L nets them. Revenue is credits less
                # debits: a credit note or a cancelled invoice's reversal debits
                # revenue, and counting only the credits charted the sale but
                # never the money given back (October read 255,942.17 against a
                # P&L of 251,692.17). Expenses are debits less credits for the
                # same reason, and year-end closing entries are left out — they
                # empty the accounts, they are not trading.
                month_lines = JournalEntryLine.objects.filter(
                    account__company=company,
                    journal_entry__posting_date__gte=m,
                    journal_entry__posting_date__lt=next_m,
                    journal_entry__status="POSTED",
                ).exclude(journal_entry__entry_type="CLOSING")
                r = month_lines.filter(account__account_type="REVENUE").aggregate(
                    dr=Sum("debit_amount"), cr=Sum("credit_amount"))
                rev = (r["cr"] or 0) - (r["dr"] or 0)
                e = month_lines.filter(account__account_type="EXPENSE").aggregate(
                    dr=Sum("debit_amount"), cr=Sum("credit_amount"))
                exp = (e["dr"] or 0) - (e["cr"] or 0)
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
            # Retired cash accounts included: a till closed on Wednesday still
            # held Monday's float on Monday.
            cash_accounts = Account.objects.filter(
                company=company,
                account_type__in=["CASH", "BANK"],
            )
            # Each day's point is the balance at the close of that day, so the
            # line moves on the days cash moved instead of repeating today.
            balance = sum(float(a.balance_as_at(d)) for a in cash_accounts)
        except Exception:
            balance = 0
        cash_data.append(balance)

    return JsonResponse({"labels": labels, "data": cash_data})


@login_required
def global_search(request):
    """Topbar search: one box, every record in the active company."""
    from django.db.models import Q
    from django.urls import reverse

    q = request.GET.get("q", "").strip()
    company = request.active_company
    groups = []
    may = page_rights(request)

    if q and company:
        from apps.accounting.models import JournalEntry
        from apps.banking.models import BankTransaction
        from apps.crm.models import Customer, Supplier
        from apps.expenses.models import ExpenseClaim
        from apps.hr.models import Employee
        from apps.inventory.models import Product
        from apps.projects.models import Project, Task
        from apps.procurement.models import Bill, PurchaseOrder
        from apps.sales.models import Invoice, SalesOrder

        def money(value):
            return f"{value:,.2f}" if value is not None else ""

        def group(label, qs, contains, url_name, title, subtitle):
            """`contains` names the icontains-able fields for this model.

            A result type is searched only for a role that may open the record
            it links to: the search box must not be a back door into a module
            the role's menu leaves out, with the amounts printed alongside.
            """
            if not may(url_name):
                return
            lookup = Q()
            for field in contains:
                lookup |= Q(**{f"{field}__icontains": q})
            items = [
                {"title": title(o), "subtitle": subtitle(o),
                 "url": reverse(url_name, args=[o.pk])}
                for o in qs.filter(lookup)[:8]
            ]
            if items:
                groups.append({"label": label, "items": items})

        group("Invoices",
              Invoice.objects.filter(company=company).select_related("customer").order_by("-invoice_date"),
              ["reference", "customer__name"], "sales:invoice_detail",
              lambda o: o.reference,
              lambda o: f"{o.customer.name} · {o.invoice_date:%d %b %Y} · {money(o.total_amount)} {o.currency}")
        group("Sales orders",
              SalesOrder.objects.filter(company=company).select_related("customer").order_by("-order_date"),
              ["reference", "customer__name"], "sales:order_detail",
              lambda o: o.reference,
              lambda o: f"{o.customer.name} · {money(o.total_amount)}")
        group("Bills",
              Bill.objects.filter(company=company).select_related("supplier").order_by("-bill_date"),
              ["reference", "supplier__name"], "procurement:bill_detail",
              lambda o: o.reference,
              lambda o: f"{o.supplier.name} · {o.bill_date:%d %b %Y} · {money(o.total_amount)} {o.currency}")
        group("Purchase orders",
              PurchaseOrder.objects.filter(company=company).select_related("supplier").order_by("-order_date"),
              ["reference", "supplier__name"], "procurement:po_detail",
              lambda o: o.reference,
              lambda o: f"{o.supplier.name} · {money(o.total_amount)}")
        group("Journal entries",
              JournalEntry.objects.filter(company=company).order_by("-posting_date"),
              ["reference"], "accounting:journal_entry_detail",
              lambda o: o.reference,
              lambda o: f"{o.get_entry_type_display()} · {o.posting_date:%d %b %Y} · {o.get_status_display()}")
        group("Customers",
              Customer.objects.filter(company=company).order_by("name"),
              ["name", "email", "phone", "tax_id"], "crm:customer_detail",
              lambda o: o.name,
              lambda o: o.email or o.phone or "")
        group("Suppliers",
              Supplier.objects.filter(company=company).order_by("name"),
              ["name", "email", "phone", "tax_id"], "crm:supplier_detail",
              lambda o: o.name,
              lambda o: o.email or o.phone or "")
        group("Products",
              Product.objects.filter(company=company).order_by("name"),
              ["name", "sku", "barcode"], "inventory:product_detail",
              lambda o: o.name,
              lambda o: f"SKU {o.sku}")
        group("Projects",
              Project.objects.filter(company=company).order_by("name"),
              ["name", "reference"], "projects:project_detail",
              lambda o: o.name,
              lambda o: f"{o.reference} · {o.get_status_display()}")
        group("Tasks",
              Task.objects.filter(project__company=company).select_related("project"),
              ["title"], "projects:task_detail",
              lambda o: o.title,
              lambda o: o.project.name)
        # Claims: only the ones the claims list itself would show this login
        # (everyone's for finance and HR, otherwise the person's own).
        from apps.expenses.views import visible_claims
        group("Expense claims",
              visible_claims(request).select_related("employee"),
              ["reference", "employee__first_name", "employee__last_name"], "expenses:claim_detail",
              lambda o: o.reference,
              lambda o: f"{o.employee.full_name} · {money(o.total_amount)}")
        group("Employees",
              Employee.objects.filter(company=company).order_by("last_name", "first_name"),
              ["first_name", "last_name", "employee_number", "job_title"], "hr:employee_detail",
              lambda o: o.full_name,
              lambda o: f"{o.employee_number} · {o.job_title}")
        group("Bank transactions",
              BankTransaction.objects.filter(bank_account__company=company).select_related("bank_account").order_by("-transaction_date"),
              ["description", "reference"], "banking:transaction_detail",
              lambda o: o.description or o.reference or f"{o.transaction_type} {money(o.amount)}",
              lambda o: f"{o.bank_account.name} · {o.transaction_date:%d %b %Y} · {money(o.amount)}")

    ctx = {
        "query": q,
        "groups": groups,
        "page_title": "Search",
        "result_count": sum(len(g["items"]) for g in groups),
        "company_name": company.name if company else "",
    }
    return render(request, "dashboard/search.html", ctx)
