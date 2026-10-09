"""Add journal_entry FK to ExpenseClaim for GL integration."""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounting", "0001_initial"),
        ("expenses", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="expenseclaim",
            name="journal_entry",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="accounting.journalentry",
            ),
        ),
    ]
