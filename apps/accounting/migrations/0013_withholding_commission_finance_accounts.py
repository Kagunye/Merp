"""Give companies already trading the accounts the default chart gained.

New companies get them from apps.core.services.CHART_OF_ACCOUNTS. This adds
only the codes a book does not have yet: it renames nothing and moves nothing,
so an account a bookkeeper has already renamed or used for something else is
left as it is. A code already in use (1360 kept for, say, a staff loan) is
skipped rather than duplicated.

It runs in every database `migrate` is pointed at (the shared one, and each
company database through migrate_companies), and it works only on companies
whose chart lives in that database. A company that moved to its own database
has no accounts left in the shared one, so it is not given a second chart there.
"""
from django.db import migrations

NEW_ACCOUNTS = [
    ("1360", "Withholding VAT Receivable 2%", "ASSET", "1000"),
    ("1370", "Withholding Tax Recoverable", "ASSET", "1000"),
    ("1371", "WHT Recoverable 3% - Contractual Fees", "ASSET", "1370"),
    ("1372", "WHT Recoverable 5% - Professional & Management Fees", "ASSET", "1370"),
    ("1373", "WHT Recoverable 7.5% - Rent", "ASSET", "1370"),
    ("2220", "Withholding VAT Payable 2%", "LIABILITY", "2000"),
    ("2230", "Withholding Tax Payable", "LIABILITY", "2000"),
    ("2231", "WHT Payable 3% - Contractual Fees", "LIABILITY", "2230"),
    ("2232", "WHT Payable 5% - Professional & Management Fees", "LIABILITY", "2230"),
    ("2233", "WHT Payable 7.5% - Rent", "LIABILITY", "2230"),
    ("4500", "Commission Income", "REVENUE", "4000"),
    ("6550", "Commissions Paid", "EXPENSE", "6000"),
    ("7210", "Interest on Lease Liabilities", "EXPENSE", "7000"),
    ("7220", "Loan Arrangement Fees", "EXPENSE", "7000"),
]


def add_accounts(apps, schema_editor):
    db = schema_editor.connection.alias
    Account = apps.get_model("accounting", "Account").objects.db_manager(db)
    company_ids = set(Account.values_list("company_id", flat=True).distinct())
    for company_id in company_ids:
        existing = {a.code: a for a in Account.filter(company_id=company_id)}
        for code, name, account_type, parent_code in NEW_ACCOUNTS:
            if code in existing:
                continue
            existing[code] = Account.create(
                company_id=company_id, code=code, name=name,
                account_type=account_type, parent=existing.get(parent_code))


def remove_accounts(apps, schema_editor):
    db = schema_editor.connection.alias
    Account = apps.get_model("accounting", "Account").objects.db_manager(db)
    # Children first, and only accounts nothing was ever posted to.
    for code, *_ in reversed(NEW_ACCOUNTS):
        Account.filter(code=code, journal_lines__isnull=True,
                       children__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0012_account_is_system'),
    ]

    operations = [
        migrations.RunPython(add_accounts, remove_accounts),
    ]
