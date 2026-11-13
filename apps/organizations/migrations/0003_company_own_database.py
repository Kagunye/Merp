"""Company.own_database and where its URL comes from (apps.core.tenancy).

Wrapped in IfMissing so a database that already has the columns (a branch
applied under another name) carries on rather than stopping.
"""
from django.db import migrations, models

from apps.core.migration_ops import IfMissing


class Migration(migrations.Migration):

    dependencies = [
        ('organizations', '0002_document_layout'),
    ]

    operations = [
        IfMissing(migrations.AddField(
            model_name='company',
            name='own_database',
            field=models.BooleanField(default=False, editable=False),
        )),
        IfMissing(migrations.AddField(
            model_name='company',
            name='database_url_env',
            field=models.CharField(blank=True, default='', editable=False, max_length=100),
        )),
        IfMissing(migrations.AddField(
            model_name='company',
            name='database_url_encrypted',
            field=models.TextField(blank=True, default='', editable=False),
        )),
    ]
