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
    # 1200 is the main bank account, the one the till, payroll and asset
    # purchases fall back to. Every further bank account gets its own ledger
    # account under it, 1201 to 1299 (see next_bank_ledger_code).
    ("1200", "Bank Accounts", "BANK", "1000"),
    ("1300", "Accounts Receivable", "RECEIVABLE", "1000"),
    ("1350", "FX Revaluation — Receivables", "ASSET", "1000"),
    # Tax a customer withheld from what it paid us: ours to claim back from
    # KRA, against VAT payable (withholding VAT) or income tax (WHT), on the
    # certificate the customer files. One account per rate, as iTax reports
    # them.
    ("1360", "Withholding VAT Receivable 2%", "ASSET", "1000"),
    ("1370", "Withholding Tax Recoverable", "ASSET", "1000"),
    ("1371", "WHT Recoverable 3% - Contractual Fees", "ASSET", "1370"),
    ("1372", "WHT Recoverable 5% - Professional & Management Fees", "ASSET", "1370"),
    ("1373", "WHT Recoverable 7.5% - Rent", "ASSET", "1370"),
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
    # Tax we withheld from a supplier's payment: owed to KRA by the 20th of
    # the following month, whatever the supplier is owed.
    ("2220", "Withholding VAT Payable 2%", "LIABILITY", "2000"),
    ("2230", "Withholding Tax Payable", "LIABILITY", "2000"),
    ("2231", "WHT Payable 3% - Contractual Fees", "LIABILITY", "2230"),
    ("2232", "WHT Payable 5% - Professional & Management Fees", "LIABILITY", "2230"),
    ("2233", "WHT Payable 7.5% - Rent", "LIABILITY", "2230"),
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
    # 4000 is the heading over every income account, not a place to post: the
    # sales of goods go to 4100. It was called "Revenue", which read as a twin
    # of 4100 Sales Revenue.
    ("4000", "Income", "REVENUE", None),
    ("4100", "Sales Revenue", "REVENUE", "4000"),
    ("4200", "Service Revenue", "REVENUE", "4000"),
    ("4300", "Other Income", "REVENUE", "4000"),
    ("4310", "Realised Exchange Gain", "REVENUE", "4300"),
    ("4320", "Unrealised Exchange Gain", "REVENUE", "4300"),
    ("4350", "Gain on Disposal of Assets", "REVENUE", "4300"),
    ("4400", "Interest Income", "REVENUE", "4000"),
    ("4500", "Commission Income", "REVENUE", "4000"),
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
    ("6550", "Commissions Paid", "EXPENSE", "6000"),
    ("6600", "Office Supplies", "EXPENSE", "6000"),
    ("6700", "Repairs & Maintenance", "EXPENSE", "6000"),
    ("6800", "Professional Fees", "EXPENSE", "6000"),
    ("6900", "Depreciation", "EXPENSE", "6000"),
    ("6950", "Loss on Disposal of Assets", "EXPENSE", "6000"),
    # Finance costs are what borrowing costs: interest on loans, overdrafts
    # and leases, and the fees for arranging them. Bank charges and exchange
    # losses sit here too, as most Kenyan books keep them. Tax penalties do
    # not belong here: they are a separate, non-deductible expense.
    ("7000", "Finance Costs", "EXPENSE", None),
    ("7100", "Bank Charges", "EXPENSE", "7000"),
    ("7200", "Interest on Loans & Overdrafts", "EXPENSE", "7000"),
    ("7210", "Interest on Lease Liabilities", "EXPENSE", "7000"),
    ("7220", "Loan Arrangement Fees", "EXPENSE", "7000"),
    ("7300", "Realised Exchange Loss", "EXPENSE", "7000"),
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


BANK_PARENT_CODE = "1200"
BANK_SUB_CODES = range(1201, 1300)


def next_bank_ledger_code(company):
    """The first free code from 1201 to 1299 for a bank's own ledger account.

    Each bank account has a ledger account of its own, kept under 1200 so the
    chart lists every bank together. Returns None once all 99 are taken.
    """
    from apps.accounting.models import Account

    taken = set(Account.objects.filter(
        company=company, code__in=[str(c) for c in BANK_SUB_CODES],
    ).values_list("code", flat=True))
    return next((str(c) for c in BANK_SUB_CODES if str(c) not in taken), None)


def create_bank_ledger_account(company, name, currency=""):
    """Open the next 1201-1299 Bank account under 1200, named for the bank,
    held in the bank's currency when that is not the book's."""
    from django.core.exceptions import ValidationError
    from apps.accounting.models import Account

    code = next_bank_ledger_code(company)
    if code is None:
        raise ValidationError(
            "All the bank ledger codes from 1201 to 1299 are in use. Add this bank's "
            "ledger account to the chart of accounts by hand and choose it here.")
    parent = Account.objects.filter(company=company, code=BANK_PARENT_CODE).first()
    return Account.objects.create(
        company=company, code=code, name=name[:200], account_type="BANK",
        parent=parent, is_reconcilable=True,
        currency="" if (currency or "").upper() in ("", (company.currency or "KES").upper())
        else currency.upper())


def create_chart_of_accounts(company):
    """Idempotently create the default chart. Returns {code: Account}."""
    from apps.accounting.models import SYSTEM_ACCOUNT_CODES, Account

    by_code = {}
    for code, name, acct_type, parent_code in CHART_OF_ACCOUNTS:
        parent = by_code.get(parent_code) if parent_code else None
        # The accounts the ledger posts to by itself are flagged as such, so the
        # chart refuses to retire them (see Account.retire_refusal).
        account, _ = Account.objects.get_or_create(
            company=company, code=code,
            defaults={"name": name, "account_type": acct_type, "parent": parent,
                      "is_system": code in SYSTEM_ACCOUNT_CODES},
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


def fiscal_year_name(start, end):
    """What a fiscal year is called: "FY 2026" or "FY 2025/26".

    A year that starts and ends inside one calendar year (January to December)
    is named after that year alone. Calling it "FY 2026/27" told every reader
    that it ran into 2027, which it does not. Only a year that straddles two
    calendar years carries both.
    """
    if start.year == end.year:
        return f"FY {start.year}"
    return f"FY {start.year}/{str(end.year)[-2:]}"


def create_fiscal_year(company):
    """Idempotently create the current fiscal year with 12 open monthly periods."""
    from apps.organizations.models import AccountingPeriod, FiscalYear

    today = datetime.date.today()
    start_month = company.fiscal_year_start_month or 1
    year = today.year if today.month >= start_month else today.year - 1
    start = datetime.date(year, start_month, 1)
    end = datetime.date(year + 1, start_month, 1) - datetime.timedelta(days=1)

    # Found by its dates rather than its name: a book provisioned before the
    # naming was corrected holds this year as "FY 2026/27", and looking it up
    # by the new name would make a second, overlapping year beside it.
    fy = (FiscalYear.objects.filter(company=company, start_date=start).first()
          or FiscalYear.objects.get_or_create(
              company=company, name=fiscal_year_name(start, end),
              defaults={"start_date": start, "end_date": end, "is_current": True},
          )[0])
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
