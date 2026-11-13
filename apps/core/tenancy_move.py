"""Moving one company's data between the shared database and its own.

Used by `manage.py move_company` (and test_fix_tenancy). Which rows belong to
the company is worked out from the foreign keys, not from a list kept by
hand, so a new app is carried along without anyone remembering to add it:

* a row with a foreign key to the company is the company's;
* a row with a foreign key to a row of the company's is the company's too
  (invoice lines, payslip lines, the many-to-many tables), repeated until
  nothing new turns up;
* tables with no foreign keys at all and no company (currencies) are shared
  reference data, copied across whole where rows are missing;
* the control tables (users, companies, memberships, sign-in) stay in the
  shared database; the company's database gets copies of the ones it needs
  (tenancy.sync_company_rows).

Rows are copied as they are -- same keys, same timestamps -- inside one
transaction on the target with foreign keys checked at the end, then counted
and the ledger compared, table by table and account by account, before the
company is switched over. Nothing is deleted from the source unless asked.
"""
from collections import defaultdict
from decimal import Decimal

from django.apps import apps
from django.core.management.color import no_style
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db.models import Count, Sum

from . import tenancy

CHUNK = 500


class MoveError(Exception):
    pass


def _chunks(values, n=CHUNK):
    values = list(values)
    for i in range(0, len(values), n):
        yield values[i:i + n]


def business_models():
    """Every concrete table outside the control database."""
    out = []
    for m in apps.get_models(include_auto_created=True):
        meta = m._meta
        if meta.proxy or not meta.managed or meta.swapped or tenancy.is_control(m):
            continue
        out.append(m)
    return out


def _relations(model):
    return [f for f in model._meta.local_concrete_fields if f.is_relation and f.related_model is not None]


def classify():
    """(scoped, reference): models that belong to a company through their
    foreign keys, and keyless shared tables copied whole."""
    Company = apps.get_model("organizations", "Company")
    models = business_models()
    scoped = set()
    changed = True
    while changed:
        changed = False
        for m in models:
            if m in scoped:
                continue
            for f in _relations(m):
                if f.related_model is Company or f.related_model in scoped:
                    scoped.add(m)
                    changed = True
                    break
    reference = [m for m in models if m not in scoped and not _relations(m)]
    return [m for m in models if m in scoped], reference


def owned_rows(company, alias):
    """{model: set of primary keys} of the company's rows in `alias`."""
    Company = apps.get_model("organizations", "Company")
    scoped, _ = classify()
    scoped_set = set(scoped)
    owned = defaultdict(set)
    delta = defaultdict(set)
    for m in scoped:
        for f in _relations(m):
            if f.related_model is Company:
                pks = m._base_manager.using(alias).filter(**{f.attname: company.pk}).values_list("pk", flat=True)
                for pk in pks:
                    if pk not in owned[m]:
                        owned[m].add(pk)
                        delta[m].add(pk)
    while delta:
        new = defaultdict(set)
        for m in scoped:
            for f in _relations(m):
                target = f.related_model
                if target not in scoped_set or not delta.get(target):
                    continue
                for chunk in _chunks(delta[target]):
                    pks = m._base_manager.using(alias).filter(
                        **{f"{f.name}__pk__in": chunk}).values_list("pk", flat=True)
                    for pk in pks:
                        if pk not in owned[m]:
                            owned[m].add(pk)
                            new[m].add(pk)
        delta = {k: v for k, v in new.items() if v}
    return {m: pks for m, pks in owned.items() if pks}


def _rows(model, pks, alias):
    for chunk in _chunks(pks):
        yield from model._base_manager.using(alias).filter(pk__in=chunk)


def _count(model, pks, alias):
    return sum(model._base_manager.using(alias).filter(pk__in=chunk).count() for chunk in _chunks(pks))


def ledger(company, alias):
    """{account id: (lines, debit, credit)} for the company's journal lines in `alias`."""
    Line = apps.get_model("accounting", "JournalEntryLine")
    out = {}
    rows = (Line._base_manager.using(alias).filter(journal_entry__company_id=company.pk)
            .values("account_id").annotate(n=Count("pk"), dr=Sum("debit_amount"), cr=Sum("credit_amount")))
    for r in rows:
        out[str(r["account_id"])] = (r["n"], r["dr"] or Decimal("0"), r["cr"] or Decimal("0"))
    return out


def ledger_totals(book):
    return (sum(v[0] for v in book.values()), sum((v[1] for v in book.values()), Decimal("0")),
            sum((v[2] for v in book.values()), Decimal("0")))


def _control_refs(owned, alias):
    """{control model: set of keys} the company's rows point at."""
    refs = defaultdict(set)
    for m, pks in owned.items():
        for f in _relations(m):
            if tenancy.is_control(f.related_model):
                for chunk in _chunks(pks):
                    for v in m._base_manager.using(alias).filter(pk__in=chunk).values_list(f.attname, flat=True):
                        if v is not None:
                            refs[f.related_model].add(v)
    return refs


def _copy_control(company, owned, source, target):
    """The control rows the company's data points at, copied into a company database."""
    from apps.accounts.models import User
    from apps.organizations.models import Branch, Company, CompanyMembership, Department
    refs = _control_refs(owned, source)
    refs[Company].add(company.pk)
    members = list(CompanyMembership._base_manager.using(DEFAULT_DB_ALIAS).filter(company_id=company.pk))
    refs[User].update(m.user_id for m in members)
    for model, pks in refs.items():
        if model is CompanyMembership:
            continue
        rows = list(_rows(model, pks, DEFAULT_DB_ALIAS))
        if model is User:
            rows = [tenancy._user_copy(u) for u in rows]
        elif model is Company:
            rows = [tenancy._company_copy(c) for c in rows]
        tenancy.upsert(model, rows, target)
    return members


def _fix_memberships(company, owned, alias):
    """Memberships in the shared database may point at a branch or department
    that no longer exists in the company's data: clear those."""
    from apps.organizations.models import Branch, CompanyMembership, Department
    qs = CompanyMembership._base_manager.using(alias).filter(company_id=company.pk)
    qs.filter(branch__isnull=False).exclude(branch_id__in=list(owned.get(Branch, ()))).update(branch=None)
    qs.filter(department__isnull=False).exclude(department_id__in=list(owned.get(Department, ()))).update(department=None)


def _copy_reference(reference, source, target):
    """Shared reference rows (currencies) missing from the target are copied.
    One already there under another key -- the same currency code, seeded by
    a migration -- is used instead: {model: {source key: target key}}."""
    remap = {}
    for m in reference:
        unique = [f for f in m._meta.local_concrete_fields if f.unique and not f.primary_key]
        have = {}
        for row in m._base_manager.using(target).all():
            have[("pk", row.pk)] = row.pk
            for f in unique:
                have[(f.attname, getattr(row, f.attname))] = row.pk
        rows, keys = [], {}
        for row in m._base_manager.using(source).all():
            if ("pk", row.pk) in have:
                continue
            match = next((have[(f.attname, getattr(row, f.attname))] for f in unique
                          if (f.attname, getattr(row, f.attname)) in have), None)
            if match is not None:
                keys[row.pk] = match
            else:
                rows.append(row)
        tenancy.insert_raw(m, rows, target)
        if keys:
            remap[m] = keys
    return remap


def _remapped(model, rows, remap):
    fks = [f for f in _relations(model) if f.related_model in remap]
    for row in rows:
        for f in fks:
            v = getattr(row, f.attname)
            if v in remap[f.related_model]:
                setattr(row, f.attname, remap[f.related_model][v])
        yield row


def _reset_sequences(models, alias):
    conn = connections[alias]
    sql = conn.ops.sequence_reset_sql(no_style(), list(models))
    if sql:
        with conn.cursor() as cursor:
            for line in sql:
                cursor.execute(line)


def _delete(owned, alias, keep=()):
    conn = connections[alias]
    for m, pks in owned.items():
        if m._meta.label_lower in keep:
            continue
        for chunk in _chunks(pks):
            m._base_manager.using(alias).filter(pk__in=chunk)._raw_delete(alias)
    return conn


def copy_company(company, source, target, owned, out=print):
    """Copy the company's rows from `source` into `target` (one transaction)."""
    from apps.organizations.models import Branch, CompanyMembership, Department
    _, reference = classify()
    tables = [m._meta.db_table for m in owned] + [m._meta.db_table for m in reference]
    conn = connections[target]
    with transaction.atomic(using=target):
        with conn.constraint_checks_disabled():
            if target == DEFAULT_DB_ALIAS:
                # Back to shared: stale copies left there when it moved out go first.
                stale = owned_rows(company, DEFAULT_DB_ALIAS)
                if stale:
                    out(f"  removing {sum(len(v) for v in stale.values())} stale rows left in the shared database")
                    _delete(stale, DEFAULT_DB_ALIAS)
                members = None
            else:
                members = _copy_control(company, owned, source, target)
            remap = _copy_reference(reference, source, target)
            for m, pks in owned.items():
                tenancy.insert_raw(m, _remapped(m, _rows(m, pks, source), remap), target)
            if members is not None:
                tenancy._upsert_memberships(members, target, Branch, Department, CompanyMembership)
                tables.append(CompanyMembership._meta.db_table)
            else:
                _fix_memberships(company, owned, target)
        conn.check_constraints(table_names=tables)
        _reset_sequences(list(owned) + reference, target)
        # Checked before the transaction commits: a copy that doesn't match is
        # rolled back and the target is left as it was.
        problems = verify(company, source, target, owned)
        if problems:
            raise MoveError("The copy does not match the source:\n  " + "\n  ".join(problems))


def verify(company, source, target, owned):
    """Problems found comparing the copy with the source ([] when it matches)."""
    problems = []
    for m, pks in owned.items():
        n = _count(m, pks, target)
        if n != len(pks):
            problems.append(f"{m._meta.label}: {len(pks)} rows in the source, {n} in the copy")
    a, b = ledger(company, source), ledger(company, target)
    if a != b:
        diff = [k for k in set(a) | set(b) if a.get(k) != b.get(k)]
        problems.append(f"ledger differs on {len(diff)} account(s): source {ledger_totals(a)}, copy {ledger_totals(b)}")
    return problems


def summary(owned):
    return sorted(((m._meta.label, len(pks)) for m, pks in owned.items()), key=lambda r: r[0])


def move_out(company, url=None, env_var=None, purge_source=False, dry_run=False, out=print):
    """Copy the company's data into its own database and switch it over."""
    from apps.organizations.models import Company
    company = Company.objects.using(DEFAULT_DB_ALIAS).get(pk=company.pk)
    if company.own_database:
        raise MoveError(f"{company} already keeps its data in its own database.")
    if env_var:
        import os
        url = os.environ.get(env_var, "")
        if not url:
            raise MoveError(f"The environment variable {env_var} is not set.")
    if not url:
        raise MoveError("Give the database URL (--url) or the environment variable that holds it (--env).")
    with tenancy.using_alias(None):
        owned = owned_rows(company, DEFAULT_DB_ALIAS)
        out(f"{company}: {sum(len(v) for v in owned.values())} rows in {len(owned)} tables")
        if dry_run:
            return {"owned": owned}
        alias = tenancy.register(company, url)
        out(f"  migrating {tenancy.describe_url(url)}")
        tenancy.migrate(alias)
        already = owned_rows(company, alias)
        if already:
            raise MoveError(f"The target database already holds {sum(len(v) for v in already.values())} "
                            f"of {company}'s rows; it must be empty of this company's data.")
        out("  copying")
        copy_company(company, DEFAULT_DB_ALIAS, alias, owned, out=out)
        tenancy.set_database_url(company, url=None if env_var else url, env_var=env_var)
        company.own_database = True
        company.save(using=DEFAULT_DB_ALIAS)
        tenancy.sync_company_rows(company, alias, force=True)
        if purge_source:
            out("  removing the company's rows from the shared database")
            conn = connections[DEFAULT_DB_ALIAS]
            with transaction.atomic(using=DEFAULT_DB_ALIAS):
                with conn.constraint_checks_disabled():
                    # Branches and departments stay: memberships point at them.
                    _delete(owned, DEFAULT_DB_ALIAS, keep=tenancy.BACK_MIRRORED)
                conn.check_constraints(table_names=[m._meta.db_table for m in owned])
    return {"owned": owned, "alias": alias}


def move_back(company, dry_run=False, out=print):
    """Copy the company's data from its own database back into the shared one."""
    from apps.organizations.models import Company
    company = Company.objects.using(DEFAULT_DB_ALIAS).get(pk=company.pk)
    if not company.own_database:
        raise MoveError(f"{company} already keeps its data in the shared database.")
    alias = tenancy.register(company)
    with tenancy.using_alias(None):
        tenancy.migrate(alias)
        owned = owned_rows(company, alias)
        out(f"{company}: {sum(len(v) for v in owned.values())} rows in {len(owned)} tables")
        if dry_run:
            return {"owned": owned}
        out("  copying back to the shared database")
        copy_company(company, alias, DEFAULT_DB_ALIAS, owned, out=out)
        company.own_database = False
        tenancy.set_database_url(company)
        company.save(using=DEFAULT_DB_ALIAS)
    tenancy.forget(alias)
    return {"owned": owned}
