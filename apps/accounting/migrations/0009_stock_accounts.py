"""Give companies founded before stock was booked the accounts it needs.

A receipt of goods nobody has billed for, materials drawn into production, and a
count that came up short each need somewhere to go. New companies take these
three codes from the default chart; a company already trading would otherwise
have its stock movements create the accounts one posting at a time, with nothing
on the ledger to say why they suddenly appeared.
"""
from django.db import migrations

STOCK_ACCOUNTS = [
    ("1450", "Work In Progress", "ASSET", "1000"),
    ("2110", "Stock Received Not Invoiced", "LIABILITY", "2000"),
    ("5300", "Stock Adjustments & Shrinkage", "COST_OF_GOODS", "5000"),
]


def add_stock_accounts(apps, schema_editor):
    Account = apps.get_model("accounting", "Account")
    Company = apps.get_model("organizations", "Company")
    for company in Company.objects.all():
        for code, name, account_type, parent_code in STOCK_ACCOUNTS:
            if Account.objects.filter(company=company, code=code).exists():
                continue
            parent = Account.objects.filter(
                company=company, code=parent_code).first()
            Account.objects.create(
                company=company, code=code, name=name,
                account_type=account_type, parent=parent)


def remove_stock_accounts(apps, schema_editor):
    Account = apps.get_model("accounting", "Account")
    Account.objects.filter(
        code__in=[row[0] for row in STOCK_ACCOUNTS],
        journal_lines__isnull=True,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0008_revaluation_accounts'),
        ('organizations', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(add_stock_accounts, remove_stock_accounts),
    ]
