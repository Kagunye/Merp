"""Keep each shelf's value to the cent on the stock level itself.

Shelves that already hold stock start from quantity × average, rounded half-up
to the cent — the best figure there is for them before this column existed.
Every movement from here on moves the value by exactly what its journal entry
posts, so the shelves and the inventory account stay in step from now on.
"""
from decimal import ROUND_HALF_UP, Decimal

from django.db import migrations, models


def value_existing_shelves(apps, schema_editor):
    StockLevel = apps.get_model("inventory", "StockLevel")
    cent = Decimal("0.01")
    for level in StockLevel.objects.exclude(quantity=0).iterator():
        worth = ((level.quantity or Decimal("0")) * (level.avg_cost or Decimal("0"))).quantize(
            cent, rounding=ROUND_HALF_UP)
        StockLevel.objects.filter(pk=level.pk).update(value=worth)


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0003_stockmovement_purchase_order_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='stocklevel',
            name='value',
            field=models.DecimalField(decimal_places=2, default=Decimal('0'), max_digits=15),
        ),
        migrations.RunPython(value_existing_shelves, migrations.RunPython.noop),
    ]
