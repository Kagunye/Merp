"""The standard Kenyan SME chart of accounts.

The ledger service looks accounts up by these codes (1300 receivables,
2200 VAT output, 2210 VAT input, 2610 employee claims payable, ...), so a
company that loads this chart posts every document type correctly.
"""
from decimal import Decimal

from django.db import transaction

# (code, name, type, parent code)
STANDARD_CHART = [
    ("1000", "Current Assets", "ASSET", None),
    ("1100", "Cash on Hand", "CASH", "1000"),
    ("1110", "Petty Cash", "CASH", "1100"),
    ("1200", "Bank - KCB Current Account", "BANK", "1000"),
    ("1210", "Bank - Equity Current Account", "BANK", "1000"),
    ("1300", "Accounts Receivable", "RECEIVABLE", "1000"),
    ("1400", "Inventory", "ASSET", "1000"),
    ("1500", "Prepaid Expenses", "ASSET", "1000"),
    ("1600", "Fixed Assets", "ASSET", None),
    ("1610", "Furniture & Equipment", "ASSET", "1600"),
    ("1620", "Motor Vehicles", "ASSET", "1600"),
    ("1630", "Computers & Technology", "ASSET", "1600"),
    ("1690", "Accumulated Depreciation", "ASSET", "1600"),
    ("2000", "Current Liabilities", "LIABILITY", None),
    ("2100", "Accounts Payable", "PAYABLE", "2000"),
    ("2200", "VAT Payable", "TAX", "2000"),
    ("2210", "VAT Input (recoverable)", "TAX", "2000"),
    ("2300", "PAYE Payable", "LIABILITY", "2000"),
    ("2400", "NSSF Payable", "LIABILITY", "2000"),
    ("2500", "SHIF/NHIF Payable", "LIABILITY", "2000"),
    ("2600", "Accrued Expenses", "LIABILITY", "2000"),
    ("2610", "Employee Claims Payable", "LIABILITY", "2000"),
    ("2700", "Loans - Current Portion", "LIABILITY", "2000"),
    ("2800", "Long Term Liabilities", "LIABILITY", None),
    ("2810", "Bank Loans", "LIABILITY", "2800"),
    ("3000", "Equity", "EQUITY", None),
    ("3100", "Share Capital", "EQUITY", "3000"),
    ("3200", "Retained Earnings", "EQUITY", "3000"),
    ("3300", "Current Year Profit/Loss", "EQUITY", "3000"),
    ("4000", "Revenue", "REVENUE", None),
    ("4100", "Sales Revenue", "REVENUE", "4000"),
    ("4200", "Service Revenue", "REVENUE", "4000"),
    ("4300", "Other Income", "REVENUE", "4000"),
    ("4400", "Interest Income", "REVENUE", "4000"),
    ("5000", "Cost of Goods Sold", "COST_OF_GOODS", None),
    ("5100", "Direct Materials", "COST_OF_GOODS", "5000"),
    ("5200", "Direct Labour", "COST_OF_GOODS", "5000"),
    ("6000", "Operating Expenses", "EXPENSE", None),
    ("6100", "Salaries & Wages", "EXPENSE", "6000"),
    ("6110", "NSSF - Employer", "EXPENSE", "6100"),
    ("6120", "SHIF - Employer", "EXPENSE", "6100"),
    ("6200", "Rent & Rates", "EXPENSE", "6000"),
    ("6300", "Utilities", "EXPENSE", "6000"),
    ("6400", "Transport & Travel", "EXPENSE", "6000"),
    ("6500", "Marketing & Advertising", "EXPENSE", "6000"),
    ("6600", "Office Supplies", "EXPENSE", "6000"),
    ("6700", "Repairs & Maintenance", "EXPENSE", "6000"),
    ("6800", "Professional Fees", "EXPENSE", "6000"),
    ("6900", "Depreciation", "EXPENSE", "6000"),
    ("7000", "Finance Costs", "EXPENSE", None),
    ("7100", "Bank Charges", "EXPENSE", "7000"),
    ("7200", "Interest Expense", "EXPENSE", "7000"),
]


@transaction.atomic
def load_standard_chart(company):
    """Create any missing standard accounts plus the 16% VAT rate.

    Returns the number of accounts created. Existing codes are left alone.
    """
    import datetime
    from .models import Account, TaxRate

    by_code = {a.code: a for a in Account.objects.filter(company=company)}
    created = 0
    for code, name, account_type, parent_code in STANDARD_CHART:
        if code in by_code:
            continue
        by_code[code] = Account.objects.create(
            company=company, code=code, name=name, account_type=account_type,
            parent=by_code.get(parent_code) if parent_code else None,
        )
        created += 1
    if not TaxRate.objects.filter(company=company, code="VAT16").exists() and "2200" in by_code:
        TaxRate.objects.create(
            company=company, name="VAT 16%", code="VAT16", rate=Decimal("16"),
            account=by_code["2200"], effective_from=datetime.date(2013, 9, 2),
        )
    return created
