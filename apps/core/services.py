"""Company provisioning: the shared setup every new company needs before it
can transact (structure, chart of accounts, fiscal calendar, default tax)."""
import calendar
import datetime
from decimal import Decimal

# (code, name, account_type, parent_code) — Kenyan trading company default chart.
CHART_OF_ACCOUNTS = [
    # Assets
    ("1000", "Current Assets", "ASSET", None),
    ("1100", "Cash on Hand", "CASH", "1000"),
    ("1110", "Petty Cash", "CASH", "1100"),
    ("1200", "Bank", "BANK", "1000"),
    ("1300", "Accounts Receivable", "RECEIVABLE", "1000"),
    ("1350", "FX Revaluation — Receivables", "ASSET", "1000"),
    ("1400", "Inventory", "ASSET", "1000"),
    ("1450", "Work In Progress", "ASSET", "1000"),
    ("1500", "Prepaid Expenses", "ASSET", "1000"),
    ("1600", "Fixed Assets", "ASSET", None),
    ("1610", "Furniture & Equipment", "ASSET", "1600"),
    ("1620", "Motor Vehicles", "ASSET", "1600"),
    ("1630", "Computers & Technology", "ASSET", "1600"),
    ("1690", "Accumulated Depreciation", "ASSET", "1600"),
    # Liabilities
    ("2000", "Current Liabilities", "LIABILITY", None),
    ("2100", "Accounts Payable", "PAYABLE", "2000"),
    ("2110", "Stock Received Not Invoiced", "LIABILITY", "2000"),
    ("2150", "FX Revaluation — Payables", "LIABILITY", "2000"),
    ("2200", "VAT Payable", "TAX", "2000"),
    ("2210", "VAT Input", "TAX", "2000"),
    ("2300", "PAYE Payable", "LIABILITY", "2000"),
    ("2400", "NSSF Payable", "LIABILITY", "2000"),
    ("2500", "SHIF/NHIF Payable", "LIABILITY", "2000"),
    ("2550", "Housing Levy Payable", "LIABILITY", "2000"),
    ("2600", "Accrued Expenses", "LIABILITY", "2000"),
    ("2700", "Loans - Current Portion", "LIABILITY", "2000"),
    ("2800", "Long Term Liabilities", "LIABILITY", None),
    ("2810", "Bank Loans", "LIABILITY", "2800"),
    # Equity
    ("3000", "Equity", "EQUITY", None),
    ("3100", "Share Capital", "EQUITY", "3000"),
    ("3200", "Retained Earnings", "EQUITY", "3000"),
    ("3300", "Current Year Profit/Loss", "EQUITY", "3000"),
    # Revenue
    ("4000", "Revenue", "REVENUE", None),
    ("4100", "Sales Revenue", "REVENUE", "4000"),
    ("4200", "Service Revenue", "REVENUE", "4000"),
    ("4300", "Other Income", "REVENUE", "4000"),
    ("4310", "Exchange Gain", "REVENUE", "4300"),
    ("4320", "Unrealised Exchange Gain", "REVENUE", "4300"),
    ("4350", "Gain on Disposal of Assets", "REVENUE", "4300"),
    ("4400", "Interest Income", "REVENUE", "4000"),
    # Cost of goods sold
    ("5000", "Cost of Goods Sold", "COST_OF_GOODS", None),
    ("5100", "Direct Materials", "COST_OF_GOODS", "5000"),
    ("5200", "Direct Labour", "COST_OF_GOODS", "5000"),
    ("5300", "Stock Adjustments & Shrinkage", "COST_OF_GOODS", "5000"),
    # Expenses
    ("6000", "Operating Expenses", "EXPENSE", None),
    ("6100", "Salaries & Wages", "EXPENSE", "6000"),
    ("6110", "NSSF - Employer", "EXPENSE", "6100"),
    ("6120", "SHIF - Employer", "EXPENSE", "6100"),
    ("6130", "Housing Levy - Employer", "EXPENSE", "6100"),
    ("6200", "Rent & Rates", "EXPENSE", "6000"),
    ("6300", "Utilities", "EXPENSE", "6000"),
    ("6400", "Transport & Travel", "EXPENSE", "6000"),
    ("6500", "Marketing & Advertising", "EXPENSE", "6000"),
    ("6600", "Office Supplies", "EXPENSE", "6000"),
    ("6700", "Repairs & Maintenance", "EXPENSE", "6000"),
    ("6800", "Professional Fees", "EXPENSE", "6000"),
    ("6900", "Depreciation", "EXPENSE", "6000"),
    ("6950", "Loss on Disposal of Assets", "EXPENSE", "6000"),
    ("7000", "Finance Costs", "EXPENSE", None),
    ("7100", "Bank Charges", "EXPENSE", "7000"),
    ("7200", "Interest Expense", "EXPENSE", "7000"),
    ("7300", "Exchange Loss", "EXPENSE", "7000"),
    ("7310", "Unrealised Exchange Loss", "EXPENSE", "7000"),
]

# (name, abbreviation, kind, factor of the kind's base unit, is_base)
# Base units come first: a derived unit is only meaningful once the book has
# something to convert it into.
DEFAULT_UNITS = [
    ("Each", "PCS", "COUNT", "1", True),
    ("Dozen", "DZN", "COUNT", "12", False),
    ("Kilogram", "KG", "WEIGHT", "1", True),
    ("Gram", "GM", "WEIGHT", "0.001", False),
    ("Tonne", "TON", "WEIGHT", "1000", False),
    ("Litre", "LTR", "VOLUME", "1", True),
    ("Millilitre", "ML", "VOLUME", "0.001", False),
    ("Metre", "MTR", "LENGTH", "1", True),
    ("Centimetre", "CM", "LENGTH", "0.01", False),
    ("Square Metre", "SQM", "AREA", "1", True),
    ("Hour", "HR", "TIME", "1", True),
    ("Minute", "MIN", "TIME", "0.016667", False),
]


# Currencies the book may transact in. A document can only be raised in one of
# these, and only once a rate has been filed for it.
DEFAULT_CURRENCIES = [
    ("KES", "Kenyan shilling", "Ksh", 2),
    ("UGX", "Ugandan shilling", "USh", 2),
    ("TZS", "Tanzanian shilling", "TSh", 2),
    ("RWF", "Rwandan franc", "RF", 2),
    ("ETB", "Ethiopian birr", "Br", 2),
    ("USD", "US dollar", "$", 2),
    ("EUR", "Euro", "€", 2),
    ("GBP", "Pound sterling", "£", 2),
]


def create_default_currencies(company):
    """Register the currency codes this book is allowed to use.

    Currencies are shared records — a US dollar means the same thing in every
    company — so this only ever creates rows the first time. What is per
    company is the rate, and a foreign document without one cannot be booked.
    """
    from apps.accounting.models import Currency

    book = (company.currency or "KES").upper()
    codes = [(book, book, "", 2)] + [c for c in DEFAULT_CURRENCIES if c[0] != book]
    for code, name, symbol, minor in codes:
        Currency.objects.get_or_create(
            code=code, defaults={"name": name, "symbol": symbol, "minor_units": minor})


def _first_day_of_next_month(day):
    if day.month == 12:
        return datetime.date(day.year + 1, 1, 1)
    return datetime.date(day.year, day.month + 1, 1)


def create_chart_of_accounts(company):
    """Idempotently create the default chart. Returns {code: Account}."""
    from apps.accounting.models import Account

    by_code = {}
    for code, name, acct_type, parent_code in CHART_OF_ACCOUNTS:
        parent = by_code.get(parent_code) if parent_code else None
        account, _ = Account.objects.get_or_create(
            company=company, code=code,
            defaults={"name": name, "account_type": acct_type, "parent": parent},
        )
        by_code[code] = account
    return by_code


def create_default_units(company):
    """Seed the units a trading company reaches for first.

    Product records demand a unit, so a company cannot hold stock without at
    least one; the base unit of each kind is created before the units that
    convert into it.
    """
    from apps.inventory.models import UnitOfMeasure

    by_abbrev = {}
    for name, abbrev, kind, factor, is_base in DEFAULT_UNITS:
        unit, _ = UnitOfMeasure.objects.get_or_create(
            company=company, abbreviation=abbrev,
            defaults={"name": name, "kind": kind,
                      "base_factor": Decimal(str(factor)), "is_base": is_base},
        )
        by_abbrev[abbrev] = unit
    return by_abbrev


def create_fiscal_year(company):
    """Idempotently create the current fiscal year with 12 open monthly periods."""
    from apps.organizations.models import AccountingPeriod, FiscalYear

    today = datetime.date.today()
    start_month = company.fiscal_year_start_month or 1
    year = today.year if today.month >= start_month else today.year - 1
    start = datetime.date(year, start_month, 1)
    end = datetime.date(year + 1, start_month, 1) - datetime.timedelta(days=1)

    fy, _ = FiscalYear.objects.get_or_create(
        company=company, name=f"FY {year}/{str(year + 1)[-2:]}",
        defaults={"start_date": start, "end_date": end, "is_current": True},
    )
    cursor, n = start, 1
    while cursor <= fy.end_date:
        last_day = calendar.monthrange(cursor.year, cursor.month)[1]
        period_end = min(datetime.date(cursor.year, cursor.month, last_day), fy.end_date)
        AccountingPeriod.objects.get_or_create(
            fiscal_year=fy, name=f"P{n:02d} {cursor.strftime('%b %Y')}",
            defaults={"start_date": cursor, "end_date": period_end, "status": "OPEN"},
        )
        cursor, n = _first_day_of_next_month(cursor), n + 1
    return fy


def provision_company(company, user):
    """Give a brand-new company the minimum structure a bookkeeper needs:
    head office, warehouse, admin membership, chart of accounts, fiscal
    calendar and the default VAT rate. Idempotent."""
    from apps.accounting.models import TaxRate
    from apps.organizations.models import (
        Branch, CompanyMembership, Department, Warehouse,
    )

    branch, _ = Branch.objects.get_or_create(
        company=company, name="Head Office",
        defaults={
            "code": "HQ", "is_headquarters": True,
            "city": company.city, "country": company.country,
        },
    )
    department, _ = Department.objects.get_or_create(
        company=company, name="Finance", defaults={"code": "FIN", "branch": branch}
    )
    Warehouse.objects.get_or_create(
        company=company, code="WH001",
        defaults={"name": "Main Warehouse", "branch": branch, "is_default": True},
    )
    if user:
        # Only the user's first company becomes their default: being
        # provisioned into another book must not hijack where they land at
        # sign-in.
        CompanyMembership.objects.get_or_create(
            company=company, user=user,
            defaults={
                "role": "ADMIN", "branch": branch, "department": department,
                "is_default": not CompanyMembership.objects.filter(
                    user=user, is_active=True).exists(),
            },
        )

    accounts = create_chart_of_accounts(company)
    create_default_currencies(company)
    units = create_default_units(company)
    fy = create_fiscal_year(company)

    vat = accounts.get("2200")
    if vat:
        TaxRate.objects.get_or_create(
            company=company, code="VAT16",
            defaults={
                "name": "VAT 16%", "rate": Decimal("16.0000"),
                "account": vat, "effective_from": fy.start_date,
            },
        )
    # Payroll needs a rates table the day the company exists: a bookkeeper who
    # runs pay before setting them would pay everybody gross.
    from apps.payroll.services import ensure_default_table
    ensure_default_table(company, fy.start_date)
    return {
        "branch": branch,
        "department": department,
        "fiscal_year": fy,
        "accounts": accounts,
        "units": units,
    }
