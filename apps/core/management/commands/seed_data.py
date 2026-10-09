"""Seed database with demo data for development."""
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Seed the database with demo company, users, and sample data"

    def handle(self, *args, **options):
        self.stdout.write("Seeding demo data...")
        self._create_admin()
        company = self._create_company()
        self._create_chart_of_accounts(company)
        self._create_sample_products(company)
        self.stdout.write(self.style.SUCCESS("✓ Demo data seeded successfully!"))
        self.stdout.write("")
        self.stdout.write("  Login credentials:")
        self.stdout.write("  Email:    admin@merp.local")
        self.stdout.write("  Password: Admin@1234")

    def _create_admin(self):
        from apps.accounts.models import User
        user, created = User.objects.get_or_create(
            email="admin@merp.local",
            defaults={
                "username": "admin",
                "first_name": "System",
                "last_name": "Administrator",
                "is_staff": True,
                "is_superuser": True,
                "is_system_admin": True,
            },
        )
        if created:
            user.set_password("Admin@1234")
            user.save()
            self.stdout.write("  ✓ Admin user created")
        return user

    def _create_company(self):
        from apps.accounts.models import User
        from apps.organizations.models import (
            Company, Branch, Department, Warehouse,
            FiscalYear, AccountingPeriod, CompanyMembership
        )
        from datetime import date

        company, created = Company.objects.get_or_create(
            name="Acme Trading Company",
            defaults={
                "legal_name": "Acme Trading Company Ltd",
                "registration_number": "PVT-2024-001",
                "tax_id": "A123456789Z",
                "currency": "KES",
                "timezone": "Africa/Nairobi",
                "address_line1": "123 Commerce Street",
                "city": "Nairobi",
                "country": "Kenya",
                "phone": "+254 700 000 000",
                "email": "info@acmetrading.co.ke",
                "industry": "Trading & Distribution",
                "is_default": True,
            },
        )
        if created:
            self.stdout.write("  ✓ Company created")

        hq, _ = Branch.objects.get_or_create(
            company=company, name="Head Office",
            defaults={"code": "HQ", "is_headquarters": True, "city": "Nairobi"},
        )
        nairobi, _ = Branch.objects.get_or_create(
            company=company, name="Nairobi Branch",
            defaults={"code": "NBI", "city": "Nairobi"},
        )

        finance, _ = Department.objects.get_or_create(company=company, name="Finance", defaults={"code": "FIN"})
        sales_dept, _ = Department.objects.get_or_create(company=company, name="Sales", defaults={"code": "SAL"})
        ops, _ = Department.objects.get_or_create(company=company, name="Operations", defaults={"code": "OPS"})
        hr_dept, _ = Department.objects.get_or_create(company=company, name="Human Resources", defaults={"code": "HR"})

        warehouse, _ = Warehouse.objects.get_or_create(
            company=company, code="WH001",
            defaults={"name": "Main Warehouse", "branch": hq, "is_default": True},
        )

        today = date.today()
        fy, _ = FiscalYear.objects.get_or_create(
            company=company, name=f"FY {today.year}",
            defaults={
                "start_date": date(today.year, 1, 1),
                "end_date": date(today.year, 12, 31),
                "is_current": True,
            },
        )

        for month in range(1, 13):
            import calendar
            last_day = calendar.monthrange(today.year, month)[1]
            AccountingPeriod.objects.get_or_create(
                fiscal_year=fy,
                name=date(today.year, month, 1).strftime("%B %Y"),
                defaults={
                    "start_date": date(today.year, month, 1),
                    "end_date": date(today.year, month, last_day),
                    "status": "CLOSED" if month < today.month else "OPEN",
                },
            )

        admin = User.objects.get(email="admin@merp.local")
        CompanyMembership.objects.get_or_create(
            company=company, user=admin,
            defaults={"role": "ADMIN", "branch": hq, "department": finance, "is_default": True},
        )

        return company

    def _create_chart_of_accounts(self, company):
        from apps.accounting.models import Account

        accounts = [
            # Assets
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
            # Liabilities
            ("2000", "Current Liabilities", "LIABILITY", None),
            ("2100", "Accounts Payable", "PAYABLE", "2000"),
            ("2200", "VAT Payable", "TAX", "2000"),
            ("2300", "PAYE Payable", "LIABILITY", "2000"),
            ("2400", "NSSF Payable", "LIABILITY", "2000"),
            ("2500", "SHIF/NHIF Payable", "LIABILITY", "2000"),
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
            ("4400", "Interest Income", "REVENUE", "4000"),
            # COGS
            ("5000", "Cost of Goods Sold", "COST_OF_GOODS", None),
            ("5100", "Direct Materials", "COST_OF_GOODS", "5000"),
            ("5200", "Direct Labour", "COST_OF_GOODS", "5000"),
            # Expenses
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

        code_to_obj = {}
        for code, name, acct_type, parent_code in accounts:
            parent = code_to_obj.get(parent_code) if parent_code else None
            obj, _ = Account.objects.get_or_create(
                company=company, code=code,
                defaults={"name": name, "account_type": acct_type, "parent": parent},
            )
            code_to_obj[code] = obj

        self.stdout.write(f"  ✓ Chart of accounts: {len(accounts)} accounts")

    def _create_sample_products(self, company):
        from apps.organizations.models import Warehouse
        from apps.inventory.models import UnitOfMeasure, ProductCategory, Product

        warehouse = Warehouse.objects.filter(company=company).first()

        uom_piece, _ = UnitOfMeasure.objects.get_or_create(
            company=company, abbreviation="PCS",
            defaults={"name": "Pieces", "is_base": True},
        )
        uom_kg, _ = UnitOfMeasure.objects.get_or_create(
            company=company, abbreviation="KG",
            defaults={"name": "Kilograms", "is_base": True},
        )
        uom_ltr, _ = UnitOfMeasure.objects.get_or_create(
            company=company, abbreviation="LTR",
            defaults={"name": "Litres", "is_base": True},
        )

        cat_electronics, _ = ProductCategory.objects.get_or_create(company=company, name="Electronics")
        cat_office, _ = ProductCategory.objects.get_or_create(company=company, name="Office Supplies")
        cat_food, _ = ProductCategory.objects.get_or_create(company=company, name="Food & Beverages")

        sample_products = [
            ("PROD-001", "Laptop Computer", cat_electronics, uom_piece, Decimal("65000"), Decimal("80000")),
            ("PROD-002", "Office Chair (Ergonomic)", cat_office, uom_piece, Decimal("8500"), Decimal("12000")),
            ("PROD-003", "A4 Copy Paper (Ream)", cat_office, uom_piece, Decimal("450"), Decimal("650")),
            ("PROD-004", "Printer Ink Cartridge", cat_electronics, uom_piece, Decimal("1200"), Decimal("1800")),
            ("PROD-005", "Mineral Water (500ml)", cat_food, uom_piece, Decimal("25"), Decimal("50")),
        ]

        for sku, name, cat, uom, cost, price in sample_products:
            Product.objects.get_or_create(
                company=company, sku=sku,
                defaults={
                    "name": name, "category": cat, "unit_of_measure": uom,
                    "cost_price": cost, "selling_price": price,
                    "reorder_level": Decimal("10"), "track_inventory": True,
                },
            )

        self.stdout.write(f"  ✓ {len(sample_products)} sample products created")
