"""Retro-fit the currency layer onto the companies already in the book.

New companies pick the exchange accounts up from the chart of accounts; a
company created last year has no 4310 or 7300, and a payment made at a better
rate than the invoice would have nowhere to put the difference.
"""
from django.db import migrations

EXCHANGE_ACCOUNTS = [
    ("4310", "Exchange Gain", "REVENUE", "4300"),
    ("7300", "Exchange Loss", "EXPENSE", "7000"),
]

CURRENCIES = [
    ("KES", "Kenyan shilling", "Ksh", 2),
    ("UGX", "Ugandan shilling", "USh", 2),
    ("TZS", "Tanzanian shilling", "TSh", 2),
    ("RWF", "Rwandan franc", "RF", 2),
    ("ETB", "Ethiopian birr", "Br", 2),
    ("USD", "US dollar", "$", 2),
    ("EUR", "Euro", "€", 2),
    ("GBP", "Pound sterling", "£", 2),
]


def add_exchange_accounts(apps, schema_editor):
    Account = apps.get_model("accounting", "Account")
    Company = apps.get_model("organizations", "Company")
    for company in Company.objects.all():
        for code, name, acct_type, parent_code in EXCHANGE_ACCOUNTS:
            if Account.objects.filter(company=company, code=code).exists():
                continue
            parent = Account.objects.filter(
                company=company, code=parent_code).first()
            Account.objects.create(
                company=company, code=code, name=name,
                account_type=acct_type, parent=parent)


def register_currencies(apps, schema_editor):
    Currency = apps.get_model("accounting", "Currency")
    Company = apps.get_model("organizations", "Company")
    codes = list(CURRENCIES)
    # A company may book in a code the region list never imagined.
    codes += [(c.currency, c.currency, "", 2) for c in Company.objects.all()
              if c.currency]
    for code, name, symbol, minor in codes:
        Currency.objects.get_or_create(
            code=code,
            defaults={"name": name, "symbol": symbol, "minor_units": minor})


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0004_currency_journalentry_exchange_rate_and_more"),
        ("organizations", "0001_initial"),
    ]
    operations = [
        migrations.RunPython(add_exchange_accounts, migrations.RunPython.noop),
        migrations.RunPython(register_currencies, migrations.RunPython.noop),
    ]
