"""Business Management launcher — a cross-module financial overview."""
from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse
from django.views.generic import TemplateView

from apps.core import access

# (section, [(route, label, icon, colour, method the tile needs)]). A tile that
# opens a form ("Voucher Entry") is shown only to a role that may also save
# it: offering a form whose Save answers 403 is a dead end, not a shortcut.
SECTIONS = [
    ("Processing", [
        ("banking:transaction_list", "Cash Book", "💰", "mint", "GET"),
        ("banking:transaction_list", "Bank Transactions", "🏦", "sky", "GET"),
        ("reporting:index", "Financial Reports", "📊", "rose", "GET"),
        # This tile opens the opening-balances screen. It used to be labelled
        # "Budget Planning", which it has never been.
        ("accounting:opening_balances", "Opening Balances", "📈", "amber", "GET"),
        ("reporting:cash_flow", "Cash Flow", "💧", "mint", "GET"),
        ("banking:home", "Reconciliation", "🔁", "sky", "GET"),
        ("accounting:journal_entry_list", "Journal Entries", "📝", "slate", "GET"),
        ("accounting:journal_entry_create", "Voucher Entry", "🧾", "peach", "POST"),
    ]),
    ("Reports", [
        ("reporting:profit_loss", "Profit & Loss", "📈", "rose", "GET"),
        ("reporting:balance_sheet", "Balance Sheet", "📊", "mint", "GET"),
        ("reporting:cash_flow", "Cash Flow Statement", "💧", "slate", "GET"),
        ("reporting:trial_balance", "Trial Balance", "⚖️", "amber", "GET"),
        ("reporting:aged_debtors", "Aged Debtors", "⏱️", "rose", "GET"),
        ("reporting:aged_creditors", "Aged Creditors", "⏱️", "peach", "GET"),
        ("reporting:index", "Financial Analysis", "🔍", "peach", "GET"),
        ("reporting:index", "Budget Reports", "📊", "sky", "GET"),
        ("organizations:company_list", "Consolidation", "🔗", "slate", "GET"),
    ]),
    ("Maintenance", [
        ("projects:project_list", "Projects / Job Costing", "📂", "rose", "GET"),
        ("banking:account_list", "Bank Configurations", "⚙️", "slate", "GET"),
        ("accounting:currency_list", "Currencies", "💱", "amber", "GET"),
        ("accounting:account_list", "Chart of Accounts", "📋", "sky", "GET"),
        ("accounting:fiscal_year_list", "Fiscal Periods", "📅", "rose", "GET"),
        ("organizations:home", "Business Parameters", "⚙️", "slate", "GET"),
        ("accounting:account_list", "Account Sets", "🗂️", "peach", "GET"),
        ("organizations:department_list", "Departments", "🏢", "sky", "GET"),
    ]),
]


class BusinessMgmtIndexView(LoginRequiredMixin, TemplateView):
    """Every finance screen on one page, showing only the ones this role opens.

    The launcher is open to everybody, but most of what it points at is not:
    an employee, a till cashier or a viewer was shown seventeen tiles that each
    answered with a 403. Each tile asks the same role table the middleware
    enforces, and a section with nothing left in it is not drawn at all.
    """
    template_name = "business_mgmt/index.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        role = getattr(self.request, "company_role", None)
        sections = []
        for title, tiles in SECTIONS:
            shown = []
            for route, label, icon, colour, method in tiles:
                namespace, _, name = route.partition(":")
                if not access.can(user, role, namespace, name, "GET"):
                    continue
                if method != "GET" and not access.can(user, role, namespace, name, method):
                    continue
                shown.append({"url": reverse(route), "label": label,
                              "icon": icon, "colour": colour})
            if shown:
                sections.append({"title": title, "tiles": shown})
        ctx["sections"] = sections
        return ctx
