"""Seed a complete demo dataset across every module.

Idempotent: safe to run repeatedly — looks up by stable keys (code,
reference, email) and only creates what's missing. Each run posts a
small set of balanced transactions that touch every account bucket in
the chart of accounts (Cash, Bank, AR, AP, Revenue, Expense, Tax),
so dashboards light up with real numbers.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone


class Command(BaseCommand):
    help = "Seed a complete demo company with transactions across all modules."

    def handle(self, *args, **opts):
        # Import late so model loading errors show as command errors, not import errors.
        from apps.accounting.models import Account, JournalEntry, JournalEntryLine, TaxRate
        from apps.accounts.models import User
        from apps.banking.models import BankAccount, BankTransaction
        from apps.crm.models import Customer, Supplier, Lead
        from apps.expenses.models import ExpenseCategory
        from apps.hr.models import Employee, LeaveType
        from apps.inventory.models import Product, UnitOfMeasure
        from apps.core.services import create_default_units
        from apps.organizations.models import Branch, Company, Department, Warehouse
        from apps.procurement.models import Bill
        from apps.sales.models import CustomerPayment, Invoice, InvoiceLine

        self.stdout.write(self.style.NOTICE("Seeding demo dataset..."))

        with transaction.atomic():
            # ---- Company + Org structure ----
            company, _ = Company.objects.get_or_create(
                name="BiasharaERP Demo Co.",
                defaults={
                    "legal_name": "BiasharaERP Demo Co. Ltd",
                    "currency": "KES",
                    "tax_id": "P000000000A",
                    "industry": "Retail & Distribution",
                    "is_default": True,
                },
            )
            branch, _ = Branch.objects.get_or_create(
                company=company, name="Head Office",
                defaults={"code": "HQ", "is_headquarters": True, "manager_name": "Jane Mwangi"},
            )
            dept, _ = Department.objects.get_or_create(
                company=company, name="Operations", defaults={"code": "OPS", "branch": branch},
            )
            warehouse, _ = Warehouse.objects.get_or_create(
                company=company, code="MAIN",
                defaults={"name": "Main Warehouse", "is_default": True, "branch": branch},
            )

            # ---- Chart of accounts (every bucket covered) ----
            account_defs = [
                ("1000", "Cash on Hand",         "CASH"),
                ("1010", "Bank — Operating",     "BANK"),
                ("1100", "Accounts Receivable",  "RECEIVABLE"),
                ("1200", "Inventory",            "ASSET"),
                ("1500", "Fixed Assets",         "ASSET"),
                ("2000", "Accounts Payable",     "PAYABLE"),
                ("2200", "VAT Payable",          "TAX"),
                ("3000", "Owner's Equity",       "EQUITY"),
                ("3100", "Retained Earnings",    "EQUITY"),
                ("4000", "Sales Revenue",        "REVENUE"),
                ("4100", "Service Revenue",      "REVENUE"),
                ("5000", "Cost of Goods Sold",   "COST_OF_GOODS"),
                ("6000", "Rent Expense",         "EXPENSE"),
                ("6100", "Utilities Expense",    "EXPENSE"),
                ("6200", "Salaries Expense",     "EXPENSE"),
                ("6300", "Office Supplies",      "EXPENSE"),
            ]
            accounts = {}
            for code, name, atype in account_defs:
                acc, _ = Account.objects.get_or_create(
                    company=company, code=code,
                    defaults={"name": name, "account_type": atype},
                )
                accounts[code] = acc
            self.stdout.write(f"  Chart of accounts: {len(accounts)} accounts ready")

            # ---- Tax rate ----
            tax, _ = TaxRate.objects.get_or_create(
                company=company, code="VAT16",
                defaults={
                    "name": "VAT 16%", "rate": Decimal("16"),
                    "account": accounts["2200"],
                    "effective_from": date.today(),
                },
            )

            # ---- CRM: customer, supplier, lead ----
            customer, _ = Customer.objects.get_or_create(
                company=company, name="Acme Trading Ltd",
                defaults={"customer_type": "BUSINESS", "email": "orders@acme.co.ke",
                          "phone": "+254700111222", "credit_limit": Decimal("500000"),
                          "payment_terms_days": 30},
            )
            supplier, _ = Supplier.objects.get_or_create(
                company=company, name="Office Supplies Kenya",
                defaults={"email": "sales@officesupplies.co.ke", "phone": "+254711222333"},
            )
            Lead.objects.get_or_create(
                company=company, name="Prospect: Nairobi Retail Chain",
                defaults={"email": "info@nrc.co.ke", "status": "NEW"},
            )

            # ---- Inventory product ----
            uom = create_default_units(company)["PCS"]
            product, _ = Product.objects.get_or_create(
                company=company, sku="WDG-001",
                defaults={
                    "name": "Standard Widget",
                    "product_type": "GOODS",
                    "unit_of_measure": uom,
                    "cost_price": Decimal("500"),
                    "selling_price": Decimal("1200"),
                    "reorder_level": Decimal("10"),
                    "is_active": True,
                },
            )

            # ---- Banking ----
            bank, _ = BankAccount.objects.get_or_create(
                company=company, account_number="0100100100",
                defaults={
                    "name": "Operating Account", "bank_name": "KCB Bank",
                    "account_type": "CURRENT", "currency": "KES",
                    "gl_account": accounts["1010"],
                    "opening_balance": Decimal("250000"),
                },
            )

            # ---- HR ----
            leave_type, _ = LeaveType.objects.get_or_create(
                company=company, name="Annual Leave",
                defaults={"days_per_year": 21, "is_paid": True},
            )
            emp, _ = Employee.objects.get_or_create(
                company=company, employee_number="E001",
                defaults={
                    "first_name": "John", "last_name": "Kamau",
                    "email": "john.kamau@biashara.co.ke",
                    "phone": "+254722333444",
                    "job_title": "Operations Manager",
                    "department": dept, "branch": branch,
                    "status": "ACTIVE", "date_hired": date.today() - timedelta(days=365),
                },
            )

            # ---- Expense category ----
            ExpenseCategory.objects.get_or_create(
                company=company, name="Travel",
                defaults={"gl_account": accounts["6300"]},
            )

            # ---- Transactions: GL entries touching every account bucket ----
            today = date.today()

            def post_je(ref, entry_type, narration, lines):
                """Create a posted journal entry if the reference doesn't exist yet."""
                je, created = JournalEntry.objects.get_or_create(
                    company=company, reference=ref,
                    defaults={
                        "entry_type": entry_type,
                        "posting_date": today,
                        "notes": narration,
                        "status": "POSTED",
                        "posted_at": timezone.now(),
                    },
                )
                if not created:
                    return je
                for acc_code, dr, cr in lines:
                    JournalEntryLine.objects.create(
                        journal_entry=je, account=accounts[acc_code],
                        description=narration,
                        debit_amount=dr, credit_amount=cr,
                    )
                return je

            # 1. Opening balance — Owner's equity funds the bank account
            post_je("OPN-001", "OPENING", "Opening capital contribution", [
                ("1010", Decimal("250000"), Decimal("0")),   # Dr Bank
                ("3000", Decimal("0"),      Decimal("250000"))  # Cr Owner's Equity
            ])

            # 2. Customer invoice — AR + Revenue + VAT
            invoice, inv_created = Invoice.objects.get_or_create(
                company=company, reference="INV-001",
                defaults={
                    "customer": customer, "invoice_date": today,
                    "due_date": today + timedelta(days=30),
                    "status": "SENT",
                    "subtotal": Decimal("100000"), "tax_amount": Decimal("16000"),
                    "total_amount": Decimal("116000"),
                },
            )
            if inv_created:
                InvoiceLine.objects.create(
                    invoice=invoice, product=product,
                    description="20 Widgets @ 5000",
                    quantity=Decimal("20"), unit_price=Decimal("5000"),
                )
            post_je("JE-INV-001", "INVOICE", "Invoice INV-001 — Acme Trading", [
                ("1100", Decimal("116000"), Decimal("0")),       # Dr AR
                ("4000", Decimal("0"),      Decimal("100000")),  # Cr Sales Revenue
                ("2200", Decimal("0"),      Decimal("16000")),   # Cr VAT Payable
            ])

            # 3. Customer payment — Bank + AR
            payment, _ = CustomerPayment.objects.get_or_create(
                company=company, reference="RCT-001",
                defaults={
                    "customer": customer, "payment_date": today,
                    "amount": Decimal("50000"), "payment_method": "BANK_TRANSFER",
                    "bank_account": bank,
                },
            )
            post_je("JE-RCT-001", "RECEIPT", "Partial payment from Acme Trading", [
                ("1010", Decimal("50000"), Decimal("0")),  # Dr Bank
                ("1100", Decimal("0"),     Decimal("50000"))  # Cr AR
            ])

            # 4. Supplier bill — Expense + VAT + AP
            bill, _ = Bill.objects.get_or_create(
                company=company, reference="BILL-001",
                defaults={
                    "supplier": supplier, "bill_date": today,
                    "due_date": today + timedelta(days=14),
                    "status": "RECEIVED",
                    "subtotal": Decimal("30000"), "tax_amount": Decimal("4800"),
                    "total_amount": Decimal("34800"),
                },
            )
            post_je("JE-BILL-001", "BILL", "Office supplies from Office Supplies Kenya", [
                ("6300", Decimal("30000"), Decimal("0")),  # Dr Office Supplies
                ("2200", Decimal("4800"),  Decimal("0")),  # Dr VAT Receivable (input tax)
                ("2000", Decimal("0"),     Decimal("34800"))  # Cr AP
            ])

            # 5. Bank transaction for the rent payment
            BankTransaction.objects.get_or_create(
                bank_account=bank, reference="BT-001",
                defaults={
                    "transaction_date": today, "transaction_type": "DEBIT",
                    "amount": Decimal("45000"), "description": "Monthly office rent",
                },
            )
            post_je("JE-RENT-001", "PAYMENT", "Monthly office rent paid", [
                ("6000", Decimal("45000"), Decimal("0")),  # Dr Rent Expense
                ("1010", Decimal("0"),     Decimal("45000"))  # Cr Bank
            ])

            # 6. Salaries + Utilities (payroll + utility snippet)
            post_je("JE-SAL-001", "PAYROLL", "Monthly payroll", [
                ("6200", Decimal("120000"), Decimal("0")),  # Dr Salaries
                ("1010", Decimal("0"),      Decimal("120000"))  # Cr Bank
            ])
            post_je("JE-UTL-001", "PAYMENT", "Electricity and water bills", [
                ("6100", Decimal("8500"), Decimal("0")),  # Dr Utilities
                ("1000", Decimal("0"),    Decimal("8500"))  # Cr Cash
            ])

            # 7. COGS entry — recognise cost on the invoice sale
            post_je("JE-COGS-001", "MANUAL", "COGS for INV-001", [
                ("5000", Decimal("10000"), Decimal("0")),  # Dr COGS (20 * 500)
                ("1200", Decimal("0"),     Decimal("10000"))  # Cr Inventory
            ])

        self.stdout.write(self.style.SUCCESS(
            "Demo seed complete. 7 journal entries posted across "
            "Cash, Bank, AR, AP, Revenue, COGS, Expenses, VAT and Equity."
        ))
