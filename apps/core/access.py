"""Role-based access policy.

One table decides who may view (GET/HEAD/OPTIONS) and who may change
(any other method) every page in the app, based on the user's role in the
*active* company. RoleAccessMiddleware enforces it on every request and the
sidebar reads it to hide modules a role can't open.

Rules are matched in order: the first (namespace, url-name pattern) that
fits the request wins. Company ADMINs may do everything inside their own
company; platform superusers and system admins bypass the table.
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

FINANCE = frozenset({"FINANCE_MANAGER", "ACCOUNTANT"})
SALES = frozenset({"SALES_MANAGER", "SALES_REP"})
STOCK = frozenset({"WAREHOUSE_MANAGER", "PROCUREMENT"})
HR = frozenset({"HR_MANAGER"})
OVERSIGHT = frozenset({"AUDITOR"})

# (namespace, url-name glob, roles that may view, roles that may change)
RULES: list[tuple[str, str, frozenset, frozenset]] = [
    # --- Approvals and postings: narrower than the module they sit in.
    ("procurement", "requisition_approve", NOBODY, frozenset({"FINANCE_MANAGER"})),
    ("procurement", "requisition_reject", NOBODY, frozenset({"FINANCE_MANAGER"})),
    ("procurement", "bill_pay", NOBODY, FINANCE),
    ("procurement", "bill_*", FINANCE | STOCK | OVERSIGHT | {"VIEWER"}, FINANCE | {"PROCUREMENT"}),
    ("pos", "terminal", SALES | {"POS_CASHIER"}, SALES | {"POS_CASHIER"}),
    ("pos", "session_open", SALES | {"POS_CASHIER"}, SALES | {"POS_CASHIER"}),
    ("sales", "invoice_post", NOBODY, FINANCE),
    ("sales", "invoice_cancel", NOBODY, FINANCE | {"SALES_MANAGER"}),
    ("sales", "invoice_credit_note", NOBODY, FINANCE | {"SALES_MANAGER"}),
    ("sales", "payment_*", FINANCE | SALES | OVERSIGHT, FINANCE),
    ("accounting", "fiscal_year_close", NOBODY, frozenset({"FINANCE_MANAGER"})),
    ("accounting", "fiscal_year_reopen", NOBODY, frozenset({"FINANCE_MANAGER"})),
    ("accounting", "period_*", NOBODY, frozenset({"FINANCE_MANAGER"})),
    ("payroll", "run_approve", NOBODY, frozenset({"FINANCE_MANAGER"})),
    ("hr", "leave_approve", NOBODY, HR),
    ("hr", "leave_*", EVERYONE, EVERYONE),
    ("expenses", "claim_approve", NOBODY, frozenset({"FINANCE_MANAGER"})),
    ("expenses", "claim_reject", NOBODY, frozenset({"FINANCE_MANAGER"})),
    ("expenses", "claim_pay", NOBODY, FINANCE),
    ("expenses", "category_*", FINANCE | OVERSIGHT, FINANCE),
    ("timesheets", "entry_approve", NOBODY, frozenset({"PROJECT_MANAGER", "HR_MANAGER"})),
    ("fixed_assets", "asset_dispose", NOBODY, frozenset({"FINANCE_MANAGER"})),

    # --- CRM splits by who owns the relationship.
    ("crm", "supplier_*", FINANCE | STOCK | OVERSIGHT | {"VIEWER"}, FINANCE | {"PROCUREMENT"}),
    ("crm", "*", FINANCE | SALES | OVERSIGHT | {"VIEWER", "POS_CASHIER"}, FINANCE | SALES),

    # --- Whole modules.
    ("accounting", "*", FINANCE | OVERSIGHT, FINANCE),
    ("banking", "*", FINANCE | OVERSIGHT, FINANCE),
    ("fixed_assets", "*", FINANCE | OVERSIGHT, FINANCE),
    ("reporting", "*", FINANCE | OVERSIGHT | {"SALES_MANAGER", "VIEWER"}, NOBODY),
    ("sales", "*", FINANCE | SALES | OVERSIGHT | {"VIEWER"}, FINANCE | SALES),
    ("pos", "*", FINANCE | SALES | OVERSIGHT | {"POS_CASHIER"}, SALES | {"POS_CASHIER"}),
    ("procurement", "*", FINANCE | STOCK | OVERSIGHT | {"VIEWER"},
     frozenset({"FINANCE_MANAGER", "PROCUREMENT", "WAREHOUSE_MANAGER"})),
    ("inventory", "*", FINANCE | SALES | STOCK | OVERSIGHT | {"POS_CASHIER", "VIEWER"}, STOCK),
    ("warehousing", "*", FINANCE | SALES | STOCK | OVERSIGHT | {"POS_CASHIER", "VIEWER"}, STOCK),
    ("manufacturing", "*", STOCK | OVERSIGHT | {"FINANCE_MANAGER", "PROJECT_MANAGER", "VIEWER"},
     frozenset({"WAREHOUSE_MANAGER", "PROJECT_MANAGER"})),
    ("workshop", "*", STOCK | OVERSIGHT | {"PROJECT_MANAGER", "VIEWER"},
     frozenset({"WAREHOUSE_MANAGER", "PROJECT_MANAGER"})),
    ("hr", "*", HR | OVERSIGHT | {"PAYROLL_OFFICER"}, HR),
    ("recruitment", "*", HR | OVERSIGHT, HR),
    ("payroll", "*", HR | OVERSIGHT | {"PAYROLL_OFFICER", "FINANCE_MANAGER"},
     frozenset({"HR_MANAGER", "PAYROLL_OFFICER", "FINANCE_MANAGER"})),
    ("projects", "*", EVERYONE - {"POS_CASHIER"}, frozenset({"PROJECT_MANAGER"})),
    ("sms", "*", FINANCE | SALES | {"VIEWER"}, frozenset({"SALES_MANAGER"})),
    ("business_mgmt", "*", EVERYONE, NOBODY),
    ("expenses", "*", EVERYONE, EVERYONE),
    ("timesheets", "*", EVERYONE, EVERYONE),
    ("helpdesk", "*", EVERYONE, EVERYONE),
    ("documents", "*", EVERYONE, EVERYONE),
    ("notifications", "*", EVERYONE, EVERYONE),
    ("dashboard", "kpi_widgets", FINANCE | OVERSIGHT, NOBODY),
    ("dashboard", "recent_transactions", FINANCE | OVERSIGHT, NOBODY),
    ("dashboard", "cashflow_chart", FINANCE | OVERSIGHT, NOBODY),
    ("dashboard", "sales_chart", FINANCE | OVERSIGHT | {"SALES_MANAGER"}, NOBODY),
    ("dashboard", "*", EVERYONE, EVERYONE),

    # --- Company set-up and people: admin territory.
    ("organizations", "company_create", EVERYONE, EVERYONE),
    ("organizations", "department_list", HR | {"FINANCE_MANAGER"}, NOBODY),
    ("organizations", "*", frozenset({"FINANCE_MANAGER"}), NOBODY),
    ("accounts", "user_*", NOBODY, NOBODY),
    ("control_panel", "*", NOBODY, NOBODY),
    ("", "schema", NOBODY, NOBODY),
    ("", "swagger-ui", NOBODY, NOBODY),
]

# Reachable without signing in.
PUBLIC = {
    ("", "service_worker"), ("", "offline"), ("", "home"),
    ("accounts", "login"), ("accounts", "register"), ("accounts", "logout"),
    ("accounts", "password_reset"), ("accounts", "password_reset_confirm"),
}

# Reachable by any signed-in user, even one who doesn't belong to a company yet.
SELF_SERVICE = {
    ("accounts", "profile"), ("accounts", "profile_edit"), ("accounts", "change_password"),
    ("dashboard", "index"), ("dashboard", "switch_company"),
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
    "control_panel": "Control Panel", "sms": "SMS",
}


def is_platform_admin(user) -> bool:
    return bool(user.is_superuser or getattr(user, "is_system_admin", False))


def _rule_for(namespace: str, url_name: str):
    for ns, pattern, read, write in RULES:
        if ns == namespace and fnmatchcase(url_name, pattern):
            return read, write
    return None


def can(user, role: str | None, namespace: str | None, url_name: str | None, method: str = "GET") -> bool:
    """Whether `user`, holding `role` in the active company, may perform this request."""
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
        # Anything not listed is admin-only, so a new page is never open by accident.
        return False
    read, write = rule
    return role in (read if method.upper() in SAFE_METHODS else write)


def can_view_module(user, role: str | None, namespace: str) -> bool:
    """Whether the module's home page should appear in this user's navigation."""
    return can(user, role, namespace, _home_name(namespace), "GET")


def _home_name(namespace: str) -> str:
    return {
        "dashboard": "index", "expenses": "home", "timesheets": "home", "helpdesk": "home",
        "documents": "home", "banking": "home", "pos": "home", "notifications": "list",
        "recruitment": "home", "organizations": "home", "accounts": "user_list",
    }.get(namespace, "index")
