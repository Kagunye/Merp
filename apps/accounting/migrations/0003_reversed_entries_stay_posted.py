"""Reversed journals stay POSTED (linked to their reversal by reversed_by).

Previously reversing set the original to REVERSED, which every balance query
excludes, while the mirror entry stayed POSTED — so the reversal was counted
twice. Restoring the originals to POSTED makes the pair net to zero.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    JournalEntry = apps.get_model("accounting", "JournalEntry")
    JournalEntry.objects.filter(status="REVERSED", reversed_by__isnull=False).update(status="POSTED")


def backwards(apps, schema_editor):
    JournalEntry = apps.get_model("accounting", "JournalEntry")
    JournalEntry.objects.filter(status="POSTED", reversed_by__isnull=False).update(status="REVERSED")


class Migration(migrations.Migration):
    dependencies = [("accounting", "0002_initial")]
    operations = [migrations.RunPython(forwards, backwards)]
