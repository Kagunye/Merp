"""Give companies founded before the revaluation screen the accounts it needs.

New companies pick the four codes up from the default chart; a company that has
been trading for a year has nowhere to put an unrealised difference, and its
receivables would keep sitting at a rate the period end has left behind.
"""
from django.db import migrations

REVALUATION_ACCOUNTS = [
    ("1350", "FX Revaluation — Receivables", "ASSET", "1000"),
    ("2150", "FX Revaluation — Payables", "LIABILITY", "2000"),
    ("4320", "Unrealised Exchange Gain", "REVENUE", "4300"),
    ("7310", "Unrealised Exchange Loss", "EXPENSE", "7000"),
]


def add_revaluation_accounts(apps, schema_editor):
    Account = apps.get_model("accounting", "Account")
    Company = apps.get_model("organizations", "Company")
    for company in Company.objects.all():
        for code, name, account_type, parent_code in REVALUATION_ACCOUNTS:
            if Account.objects.filter(company=company, code=code).exists():
                continue
            parent = Account.objects.filter(
                company=company, code=parent_code).first()
            Account.objects.create(
                company=company, code=code, name=name,
                account_type=account_type, parent=parent)


def remove_revaluation_accounts(apps, schema_editor):
    Account = apps.get_model("accounting", "Account")
    Account.objects.filter(
        code__in=[row[0] for row in REVALUATION_ACCOUNTS],
        journal_lines__isnull=True,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0007_alter_journalentry_entry_type'),
        ('organizations', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(add_revaluation_accounts, remove_revaluation_accounts),
    ]
