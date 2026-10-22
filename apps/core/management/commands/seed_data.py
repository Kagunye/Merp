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
        from apps.core.services import CHART_OF_ACCOUNTS, create_chart_of_accounts

        create_chart_of_accounts(company)
        self.stdout.write(f"  ✓ Chart of accounts: {len(CHART_OF_ACCOUNTS)} accounts")

    def _create_sample_products(self, company):
        from apps.organizations.models import Warehouse
        from apps.inventory.models import UnitOfMeasure, ProductCategory, Product
        from apps.core.services import create_default_units

        warehouse = Warehouse.objects.filter(company=company).first()

        units = create_default_units(company)
        uom_piece, uom_kg, uom_ltr = units["PCS"], units["KG"], units["LTR"]

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
