"""Indexes for the date filters the dashboard workspace uses that their models
do not index already (invoice_date, quote_date, order_date, bill_date and
created_at are indexed on the models themselves).

Plain SQL rather than AddIndex, because the tables belong to other apps and
this change must not touch their models: CREATE INDEX IF NOT EXISTS works on
both SQLite and PostgreSQL, and the reverse drops exactly these indexes.
"""
from django.db import migrations

INDEXES = [
    ("dash_inv_company_due", "sales_invoice", "company_id, due_date"),
    ("dash_task_assignee_due", "projects_task", "assigned_to_id, due_date"),
    ("dash_lead_company_close", "crm_lead", "company_id, expected_close_date"),
    ("dash_interview_when", "recruitment_interview", "scheduled_at"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
        ("sales", "0008_invoice_line_stock_links"),
        ("projects", "0001_initial"),
        ("crm", "0001_initial"),
        ("recruitment", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql=f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols});",
            reverse_sql=f"DROP INDEX IF EXISTS {name};",
        )
        for name, table, cols in INDEXES
    ]
