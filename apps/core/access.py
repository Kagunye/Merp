"""Role-based access policy.

One table decides who may view (GET/HEAD/OPTIONS) and who may change (any
other method) every page in the app, from the user's role in the *active*
company (CompanyMembership.role). RoleAccessMiddleware enforces it on every
request, and the sidebar reads it to hide modules a role can't open.

Rules are matched in order: the first (namespace, url-name pattern) that fits
wins. Company ADMINs may do everything inside their own company; platform
superusers and system admins bypass the table. A page that no rule mentions
is admin-only, so a new screen is never open to everyone by accident.
"""
from __future__ import annotations

from fnmatch import fnmatchcase

ROLES = frozenset({
    "ADMIN", "FINANCE_MANAGER", "ACCOUNTANT", "SALES_MANAGER", "SALES_REP",
    "PROCUREMENT", "WAREHOUSE_MANAGER", "HR_MANAGER", "PAYROLL_OFFICER",
    "PROJECT_MANAGER", "EMPLOYEE", "AUDITOR", "POS_CASHIER", "VIEWER",
})
EVERYONE = ROLES
NOBODY = frozenset()

FM = frozenset({"FINANCE_MANAGER"})
FINANCE = frozenset({"FINANCE_MANAGER", "ACCOUNTANT"})
SALES = frozenset({"SALES_MANAGER", "SALES_REP"})
STOCK = frozenset({"WAREHOUSE_MANAGER", "PROCUREMENT"})
HR = frozenset({"HR_MANAGER"})
PAYROLL = frozenset({"PAYROLL_OFFICER"})
AUDIT = frozenset({"AUDITOR"})
TILL = SALES | {"POS_CASHIER"}

# (namespace, url-name glob, roles that may view, roles that may change)
RULES: list[tuple[str, str, frozenset, frozenset]] = [
    # --- Approvals, postings and reversals: narrower than their module.
    ("accounting", "fiscal_year_*", FINANCE | AUDIT, FM),
    ("accounting", "period_*", FINANCE | AUDIT, FM),
    ("accounting", "revaluation_post", NOBODY, FM),
    ("accounting", "revaluation_reverse", NOBODY, FM),
    ("accounting", "opening_balances", FINANCE | AUDIT, FM),
    ("banking", "reconciliation_post", NOBODY, FM),
    ("banking", "reconciliation_reverse", NOBODY, FM),
    ("sales", "invoice_post", NOBODY, FINANCE),
    ("sales", "invoice_cancel", NOBODY, FINANCE | {"SALES_MANAGER"}),
    ("sales", "invoice_credit_note", NOBODY, FINANCE | {"SALES_MANAGER"}),
    ("sales", "payment_reverse", NOBODY, FM),
    ("sales", "payment_*", FINANCE | SALES | AUDIT, FINANCE),
    ("sales", "ar_aging", FINANCE | SALES | AUDIT, NOBODY),
    ("sales", "customer_statement", FINANCE | SALES | AUDIT, NOBODY),
    ("procurement", "requisition_approve", NOBODY, FM),
    ("procurement", "requisition_reject", NOBODY, FM),
    ("procurement", "bill_pay", NOBODY, FINANCE),
    ("procurement", "bill_post", NOBODY, FINANCE),
    ("procurement", "bill_cancel", NOBODY, FINANCE),
    ("procurement", "bill_debit_note", NOBODY, FINANCE),
    ("procurement", "bill_*", FINANCE | STOCK | AUDIT | {"VIEWER"}, FINANCE | {"PROCUREMENT"}),
    ("procurement", "po_to_bill", NOBODY, FINANCE | {"PROCUREMENT"}),
    ("procurement", "grn*", FINANCE | STOCK | AUDIT | {"VIEWER"}, STOCK),
    ("procurement", "ap_aging", FINANCE | STOCK | AUDIT, NOBODY),
    ("procurement", "supplier_statement", FINANCE | STOCK | AUDIT, NOBODY),
    ("pos", "terminal", TILL, TILL),
    ("pos", "session_open", TILL, TILL),
    ("pos", "session_close", NOBODY, TILL | FM),
    ("pos", "sale_refund", NOBODY, TILL | FM),
    ("payroll", "run_approve", NOBODY, FM),
    ("payroll", "run_post", NOBODY, FM),
    ("payroll", "run_cancel", NOBODY, FM),
    ("payroll", "band_*", HR | PAYROLL | FM | AUDIT, PAYROLL | FM),
    ("payroll", "rule*", HR | PAYROLL | FM | AUDIT, PAYROLL | FM),
    ("payroll", "component_*", HR | PAYROLL | FM | AUDIT, PAYROLL | FM),
    ("hr", "leave_approve", NOBODY, HR),
    ("hr", "leave_*", EVERYONE, EVERYONE),
    ("hr", "contract_*", HR | PAYROLL | AUDIT, HR),
    ("expenses", "claim_approve", NOBODY, FM),
    ("expenses", "claim_reject", NOBODY, FM),
    ("expenses", "claim_pay", NOBODY, FINANCE),
    ("expenses", "category_*", FINANCE | AUDIT, FINANCE),
    ("timesheets", "entry_approve", NOBODY, frozenset({"PROJECT_MANAGER", "HR_MANAGER"})),
    ("fixed_assets", "asset_dispose", NOBODY, FM),
    ("fixed_assets", "*_reverse", NOBODY, FM),
    ("inventory", "adjustment_create", FINANCE | STOCK | AUDIT, frozenset({"WAREHOUSE_MANAGER"})),
    ("inventory", "stock_transfer", FINANCE | STOCK | AUDIT, frozenset({"WAREHOUSE_MANAGER"})),
    ("inventory", "inventory_count", FINANCE | STOCK | AUDIT, frozenset({"WAREHOUSE_MANAGER"})),
    ("inventory", "unit_*", STOCK | FINANCE | AUDIT, STOCK),

    # --- Customers and suppliers: split by who owns the relationship.
    ("crm", "supplier_*", FINANCE | STOCK | AUDIT | {"VIEWER"}, FINANCE | {"PROCUREMENT"}),
    ("crm", "*", FINANCE | SALES | AUDIT | {"VIEWER", "POS_CASHIER"}, FINANCE | SALES),

    # --- Whole modules.
    ("accounting", "*", FINANCE | AUDIT, FINANCE),
    ("banking", "*", FINANCE | AUDIT, FINANCE),
    ("fixed_assets", "*", FINANCE | AUDIT, FINANCE),
    ("reporting", "aged_*", FINANCE | AUDIT | {"SALES_MANAGER"}, NOBODY),
    ("reporting", "*", FINANCE | AUDIT, NOBODY),
    ("sales", "*", FINANCE | SALES | AUDIT | {"VIEWER"}, SALES | {"FINANCE_MANAGER"}),
    ("pos", "*", FINANCE | TILL | AUDIT, TILL),
    ("procurement", "*", FINANCE | STOCK | AUDIT | {"VIEWER"},
     frozenset({"FINANCE_MANAGER", "PROCUREMENT", "WAREHOUSE_MANAGER"})),
    ("inventory", "*", FINANCE | SALES | STOCK | AUDIT | {"POS_CASHIER", "VIEWER"}, STOCK),
    ("warehousing", "*", FINANCE | SALES | STOCK | AUDIT | {"POS_CASHIER", "VIEWER"}, STOCK),
    ("manufacturing", "*", STOCK | AUDIT | {"FINANCE_MANAGER", "PROJECT_MANAGER", "VIEWER"},
     frozenset({"WAREHOUSE_MANAGER", "PROJECT_MANAGER"})),
    ("workshop", "*", STOCK | AUDIT | {"PROJECT_MANAGER", "VIEWER"},
     frozenset({"WAREHOUSE_MANAGER", "PROJECT_MANAGER"})),
    ("hr", "*", HR | PAYROLL | AUDIT, HR),
    ("recruitment", "*", HR | AUDIT, HR),
    ("payroll", "*", HR | PAYROLL | FM | AUDIT, PAYROLL | HR),
    ("projects", "*", EVERYONE - {"POS_CASHIER"}, frozenset({"PROJECT_MANAGER"})),
    ("sms", "*", FINANCE | SALES | {"VIEWER"}, frozenset({"SALES_MANAGER"})),
    ("business_mgmt", "*", EVERYONE, NOBODY),
    ("expenses", "*", EVERYONE, EVERYONE),
    ("timesheets", "*", EVERYONE, EVERYONE),
    ("helpdesk", "*", EVERYONE, EVERYONE),
    ("documents", "*", EVERYONE, EVERYONE),
    ("notifications", "*", EVERYONE, EVERYONE),
    ("dashboard", "kpi_widgets", FINANCE | AUDIT, NOBODY),
    ("dashboard", "recent_transactions", FINANCE | AUDIT, NOBODY),
    ("dashboard", "cashflow_chart", FINANCE | AUDIT, NOBODY),
    ("dashboard", "sales_chart", FINANCE | AUDIT | {"SALES_MANAGER"}, NOBODY),
    ("dashboard", "*", EVERYONE, EVERYONE),

    # --- Company set-up and user management: admin territory.
    ("organizations", "department_list", HR | {"FINANCE_MANAGER"}, NOBODY),
    ("organizations", "*", FM, NOBODY),
    ("accounts", "user_*", NOBODY, NOBODY),
    ("control_panel", "*", NOBODY, NOBODY),
    ("", "schema", NOBODY, NOBODY),
    ("", "swagger-ui", NOBODY, NOBODY),
]

# Reachable without signing in.
PUBLIC = {
    ("", "home"),
    ("accounts", "login"), ("accounts", "register"), ("accounts", "logout"),
    ("accounts", "password_reset"), ("accounts", "password_reset_confirm"),
}

# Reachable by any signed-in user, even one who doesn't belong to a company yet.
SELF_SERVICE = {
    ("accounts", "profile"), ("accounts", "profile_edit"), ("accounts", "change_password"),
    ("dashboard", "index"), ("dashboard", "switch_company"), ("dashboard", "switch_branch"),
    ("dashboard", "search"),
    ("organizations", "company_create"),
    ("notifications", "list"), ("notifications", "mark_read"), ("notifications", "mark_all_read"),
}

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

MODULE_LABELS = {
    "accounting": "General Ledger", "banking": "Banking", "fixed_assets": "Fixed Assets",
    "reporting": "Reports", "sales": "Sales", "pos": "Point of Sale", "procurement": "Procurement",
    "inventory": "Inventory", "warehousing": "Warehousing", "manufacturing": "Manufacturing",
    "workshop": "Workshop", "hr": "Human Resources", "recruitment": "Recruitment",
    "payroll": "Payroll", "projects": "Projects", "crm": "Customers & Suppliers",
    "organizations": "Company Settings", "accounts": "User Management",
    "control_panel": "Control Panel", "sms": "SMS", "business_mgmt": "Business Management",
}


def is_platform_admin(user) -> bool:
    return bool(user.is_superuser or getattr(user, "is_system_admin", False))


def _rule_for(namespace: str, url_name: str):
    for ns, pattern, read, write in RULES:
        if ns == namespace and fnmatchcase(url_name, pattern):
            return read, write
    return None


def can(user, role: str | None, namespace: str | None, url_name: str | None, method: str = "GET") -> bool:
    """Whether `user`, holding `role` in the active company, may make this request."""
    namespace, url_name = namespace or "", url_name or ""
    if (namespace, url_name) in PUBLIC:
        return True
    if not user.is_authenticated:
        return False
    if is_platform_admin(user):
        return True
    if (namespace, url_name) in SELF_SERVICE:
        return True
    if not role:
        return False
    if role == "ADMIN":
        return True
    rule = _rule_for(namespace, url_name)
    if rule is None:
        return False
    read, write = rule
    return role in (read if method.upper() in SAFE_METHODS else write)


HOME_NAMES = {
    "dashboard": "index", "expenses": "home", "timesheets": "home", "helpdesk": "home",
    "documents": "home", "banking": "home", "pos": "home", "notifications": "list",
    "recruitment": "home", "organizations": "home", "accounts": "user_list",
}


def can_view_module(user, role: str | None, namespace: str) -> bool:
    """Whether the module's home page should appear in this user's navigation."""
    return can(user, role, namespace, HOME_NAMES.get(namespace, "index"), "GET")
