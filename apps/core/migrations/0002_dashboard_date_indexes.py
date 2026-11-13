"""Indexes for the date filters the dashboard workspace uses that their models
do not index already (invoice_date, quote_date, order_date, bill_date and
created_at are indexed on the models themselves).

Plain SQL rather than AddIndex, because the tables belong to other apps and
this change must not touch their models. Each index is created only if it is
not there yet (SQLite, PostgreSQL and MySQL), and the reverse drops exactly these.
"""
from django.db import migrations

INDEXES = [
    ("dash_inv_company_due", "sales_invoice", "company_id, due_date"),
    ("dash_task_assignee_due", "projects_task", "assigned_to_id, due_date"),
    ("dash_lead_company_close", "crm_lead", "company_id, expected_close_date"),
    ("dash_interview_when", "recruitment_interview", "scheduled_at"),
]


def _existing(schema_editor, table):
    conn = schema_editor.connection
    with conn.cursor() as cursor:
        return set(conn.introspection.get_constraints(cursor, table))


def add_indexes(apps, schema_editor):
    # Checked first rather than CREATE INDEX IF NOT EXISTS, which MySQL lacks.
    q = schema_editor.quote_name
    for name, table, cols in INDEXES:
        if name not in _existing(schema_editor, table):
            columns = ", ".join(q(c.strip()) for c in cols.split(","))
            schema_editor.execute(f"CREATE INDEX {q(name)} ON {q(table)} ({columns})")


def drop_indexes(apps, schema_editor):
    q = schema_editor.quote_name
    for name, table, _cols in INDEXES:
        if name in _existing(schema_editor, table):
            if schema_editor.connection.vendor == "mysql":
                schema_editor.execute(f"DROP INDEX {q(name)} ON {q(table)}")
            else:
                schema_editor.execute(f"DROP INDEX {q(name)}")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
        ("sales", "0008_invoice_line_stock_links"),
        ("projects", "0001_initial"),
        ("crm", "0001_initial"),
        ("recruitment", "0001_initial"),
    ]

    operations = [migrations.RunPython(add_indexes, drop_indexes)]
