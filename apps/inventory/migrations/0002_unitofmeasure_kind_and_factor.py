# Units of measure gain a kind and a conversion factor. Existing rows are
# backfilled so every kind keeps exactly one base unit to convert through.

from decimal import Decimal

from django.db import migrations, models

KIND_BY_ABBREV = {
    "PCS": "COUNT", "PC": "COUNT", "EA": "COUNT", "UNIT": "COUNT", "NOS": "COUNT",
    "DZN": "COUNT", "DOZ": "COUNT",
    "KG": "WEIGHT", "GM": "WEIGHT", "G": "WEIGHT", "MG": "WEIGHT", "TON": "WEIGHT", "MT": "WEIGHT",
    "LTR": "VOLUME", "L": "VOLUME", "ML": "VOLUME", "Litre": "VOLUME",
    "MTR": "LENGTH", "M": "LENGTH", "CM": "LENGTH", "MM": "LENGTH", "KM": "LENGTH",
    "SQM": "AREA", "M2": "AREA", "SQFT": "AREA",
    "HR": "TIME", "HOUR": "TIME", "MIN": "TIME", "DAY": "TIME", "MTH": "TIME",
}

# How much of the kind's base unit one of these is worth.
FACTOR_BY_ABBREV = {
    "DZN": Decimal("12"), "DOZ": Decimal("12"),
    "GM": Decimal("0.001"), "MG": Decimal("0.000001"), "TON": Decimal("1000"), "MT": Decimal("1000"),
    "ML": Decimal("0.001"), "CM": Decimal("0.01"), "MM": Decimal("0.001"), "KM": Decimal("1000"),
    "SQFT": Decimal("0.092903"),
    "MIN": Decimal("0.016667"), "DAY": Decimal("24"), "MTH": Decimal("720"),
}

BASE_BY_KIND = {
    "COUNT": "PCS", "WEIGHT": "KG", "VOLUME": "LTR",
    "LENGTH": "MTR", "AREA": "SQM", "TIME": "HR", "OTHER": "PCS",
}


def backfill_units(apps, schema_editor):
    Unit = apps.get_model("inventory", "UnitOfMeasure")
    for unit in Unit.objects.all():
        abbrev = (unit.abbreviation or "").strip().upper()
        unit.kind = KIND_BY_ABBREV.get(abbrev, "COUNT" if unit.is_base else "OTHER")
        unit.base_factor = FACTOR_BY_ABBREV.get(abbrev, Decimal("1"))
        unit.is_base = abbrev == BASE_BY_KIND.get(unit.kind)
        unit.save(update_fields=["kind", "base_factor", "is_base"])

    # A kind with no base unit has nothing to convert through, so promote the
    # largest unit the company actually has for it.
    for company_id, kind in Unit.objects.values_list("company_id", "kind").distinct():
        has_base = Unit.objects.filter(
            company_id=company_id, kind=kind, is_base=True).exists()
        if has_base:
            continue
        candidate = Unit.objects.filter(company_id=company_id, kind=kind).order_by(
            "-base_factor", "abbreviation").first()
        if candidate:
            candidate.is_base = True
            candidate.base_factor = Decimal("1")
            candidate.save(update_fields=["is_base", "base_factor"])


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0001_initial"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="unitofmeasure",
            options={"ordering": ["kind", "base_factor", "name"],
                     "verbose_name": "unit of measure",
                     "verbose_name_plural": "units of measure"},
        ),
        migrations.AddField(
            model_name="unitofmeasure",
            name="base_factor",
            field=models.DecimalField(
                decimal_places=6, default=Decimal("1"), max_digits=15,
                help_text="1 of this unit equals this many of the base unit."),
        ),
        migrations.AddField(
            model_name="unitofmeasure",
            name="kind",
            field=models.CharField(
                choices=[("COUNT", "Count / pieces"), ("WEIGHT", "Weight"),
                         ("VOLUME", "Volume"), ("LENGTH", "Length"),
                         ("AREA", "Area"), ("TIME", "Time"), ("OTHER", "Other")],
                default="COUNT", max_length=10),
        ),
        migrations.RunPython(backfill_units, migrations.RunPython.noop),
    ]
