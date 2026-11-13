"""A company's business data in a database of its own.

By default every company shares one database, kept apart by the company on
each row (request.active_company and apps.core.access). That stays as it is.
A company can also be given its own database:

* The shared database is the control database. Sign-in and everything about
  who may see what stays there: users, sessions, companies, memberships,
  permissions, the audit trail of django-auditlog, Celery's tables.
* Everything else -- the books, sales, POS, stock, purchasing, HR, payroll,
  KRA, payments, and the company's branches, warehouses and fiscal years --
  lives in the company's database while one of its pages is being served.
* Every database carries the full schema, so the company's database also
  holds a copy of its Company row, its memberships and its members' User rows
  (with no usable password). Foreign keys and joins therefore keep working the
  same way on SQLite, PostgreSQL and MySQL; the copies are kept in step from
  the control database when those rows are saved.

CompanyContextMiddleware switches to the company's database once it knows the
active company; CompanyDatabaseRouter sends each query to the right place; and
transaction.atomic() with no `using` opens on the company's database while it
is active (see install()). `manage.py move_company` copies a company's data
out to its own database and back, and `manage.py migrate_companies` keeps
those databases' schema current.
"""
import base64
import contextvars
import hashlib
import logging
import os
import threading
from contextlib import contextmanager

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, connections, transaction

log = logging.getLogger(__name__)

ALIAS_PREFIX = "company_"

# Kept in the control (shared) database whatever company is active.
CONTROL_APPS = frozenset({
    "accounts", "auth", "sessions", "admin", "contenttypes", "auditlog",
    "guardian", "authtoken", "django_celery_beat", "django_celery_results",
})
CONTROL_MODELS = frozenset({"organizations.company", "organizations.companymembership"})
# Business rows that memberships in the control database point at, so a copy
# of each is kept there too.
BACK_MIRRORED = frozenset({"organizations.branch", "organizations.department"})

_current = contextvars.ContextVar("mofisi_company_db", default=None)
# While a company database is being migrated, every query (data migrations
# that don't say `using`) goes to it, control tables included.
_migrating = contextvars.ContextVar("mofisi_company_db_migrating", default=None)
_lock = threading.Lock()
_registered = {}          # alias -> URL it was registered with
_synced = set()           # (alias, model label, pk, stamp) already copied this process
_migrated = set()         # aliases whose schema was found current this process


def is_control(model):
    """A model (or row) kept in the control database."""
    meta = model._meta
    return meta.app_label in CONTROL_APPS or meta.label_lower in CONTROL_MODELS


def is_company_alias(alias):
    return bool(alias) and alias.startswith(ALIAS_PREFIX)


# ── the URL ──────────────────────────────────────────────────────────────

def _fernet():
    from cryptography.fernet import Fernet
    secret = getattr(settings, "TENANT_DB_KEY", "") or settings.SECRET_KEY
    key = base64.urlsafe_b64encode(hashlib.sha256(f"mofisi-company-db:{secret}".encode()).digest())
    return Fernet(key)


def encrypt_url(url):
    return _fernet().encrypt(url.encode()).decode()


def decrypt_url(token):
    return _fernet().decrypt(token.encode()).decode()


def company_database_url(company):
    """The company's database URL, from its environment variable or decrypted."""
    if company.database_url_env:
        return os.environ.get(company.database_url_env, "")
    if company.database_url_encrypted:
        return decrypt_url(company.database_url_encrypted)
    return ""


def set_database_url(company, url=None, env_var=None):
    """Remember where the company's database is (not saved: the caller saves)."""
    if env_var:
        company.database_url_env, company.database_url_encrypted = env_var, ""
    elif url:
        company.database_url_env, company.database_url_encrypted = "", encrypt_url(url)
    else:
        company.database_url_env = company.database_url_encrypted = ""


def describe_url(url):
    """The URL with its password hidden, for messages."""
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(url)
    if parts.password:
        netloc = parts.netloc.replace(f":{parts.password}@", ":****@")
        return urlunsplit(parts._replace(netloc=netloc))
    return url


# ── connections ──────────────────────────────────────────────────────────

def alias_for(company):
    pk = getattr(company, "pk", company)
    return f"{ALIAS_PREFIX}{getattr(pk, 'hex', str(pk).replace('-', ''))}"


def register(company, url=None):
    """Make the company's database reachable as connections[alias_for(company)]."""
    from config.dburl import database_config

    alias = alias_for(company)
    url = url or company_database_url(company)
    if not url:
        raise ImproperlyConfigured(f"{company} has no database URL set.")
    with _lock:
        if _registered.get(alias) == url:
            return alias
        if alias in _registered:          # the URL changed: drop the old connection
            try:
                connections[alias].close()
                del connections[alias]
            except Exception:
                pass
        cfg = database_config(url)
        cfg.setdefault("CONN_MAX_AGE", settings.DATABASES[DEFAULT_DB_ALIAS].get("CONN_MAX_AGE", 0))
        full = connections.configure_settings({DEFAULT_DB_ALIAS: connections.settings[DEFAULT_DB_ALIAS], alias: cfg})
        connections.settings[alias] = full[alias]
        settings.DATABASES[alias] = full[alias]
        _registered[alias] = url
        for key in [k for k in _synced if k[0] == alias]:
            _synced.discard(key)
    return alias


def forget(alias):
    """Drop a registered company database (used after a test or a move back)."""
    with _lock:
        try:
            connections[alias].close()
        except Exception:
            pass
        try:
            del connections[alias]
        except Exception:
            pass
        connections.settings.pop(alias, None)
        settings.DATABASES.pop(alias, None)
        _registered.pop(alias, None)
        _migrated.discard(alias)
        for key in [k for k in _synced if k[0] == alias]:
            _synced.discard(key)


def current_alias():
    """The company database serving the current request, or None for shared."""
    return _current.get()


@contextmanager
def using_alias(alias):
    token = _current.set(alias)
    try:
        yield alias
    finally:
        _current.reset(token)


@contextmanager
def company_db(company):
    """Run the block against the company's database (or the shared one when the
    company keeps its data there). For commands and jobs outside a request."""
    if company is None or not company.own_database:
        with using_alias(None):
            yield None
        return
    alias = register(company)
    with using_alias(alias):
        sync_company_rows(company, alias)
        yield alias


def migrate(alias, verbosity=0, stdout=None):
    """Bring a company database's schema up to date (all apps, like the shared one)."""
    from django.core.management import call_command
    token = _migrating.set(alias)
    try:
        kwargs = {"database": alias, "interactive": False, "verbosity": verbosity}
        if stdout is not None:
            kwargs["stdout"] = stdout
        call_command("migrate", **kwargs)
    finally:
        _migrating.reset(token)
    _migrated.add(alias)


def ensure_migrated(alias):
    """Once per process, migrate a company database that is behind the code --
    a deploy migrates the shared database; each company's catches up on first
    use (and `manage.py migrate_companies` does them all at once)."""
    if alias in _migrated:
        return
    from django.db.migrations.executor import MigrationExecutor
    executor = MigrationExecutor(connections[alias])
    if executor.migration_plan(executor.loader.graph.leaf_nodes()):
        log.info("Migrating company database %s", alias)
        migrate(alias)
    _migrated.add(alias)


def activate(company, user=None):
    """Switch the current request to the company's database. Returns a token
    for deactivate(), or None when the company uses the shared database."""
    if company is None or not company.own_database:
        return None
    alias = register(company)
    ensure_migrated(alias)
    token = _current.set(alias)
    sync_company_rows(company, alias)
    if user is not None and getattr(user, "pk", None):
        sync_user(user, alias)
    return token


def begin_request():
    """Start a request on the shared database; the token restores what was
    current before (None in a real server, a company in a test or job)."""
    return _current.set(None)


def deactivate(token=None):
    if token is not None:
        try:
            _current.reset(token)
            return
        except ValueError:       # set in another context
            pass
    _current.set(None)


# ── routing ──────────────────────────────────────────────────────────────

class CompanyDatabaseRouter:
    """Control tables to the shared database; the rest to the active company's.

    With no company database active it answers nothing, so Django behaves
    exactly as it does with one database.
    """

    def _route(self, model, **hints):
        migrating = _migrating.get()
        if migrating is not None:
            return migrating
        alias = _current.get()
        if alias is None:
            return None
        return DEFAULT_DB_ALIAS if is_control(model) else alias

    def db_for_read(self, model, **hints):
        return self._route(model, **hints)

    def db_for_write(self, model, **hints):
        return self._route(model, **hints)

    def allow_relation(self, obj1, obj2, **hints):
        # The copies share primary keys with the control rows, so an invoice in
        # a company database may point at a Company read from the shared one.
        db1, db2 = obj1._state.db, obj2._state.db
        if is_company_alias(db1) or is_company_alias(db2):
            return True
        if _current.get() is not None and (is_control(obj1) or is_control(obj2)):
            return True
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return None             # every database carries the full schema


_original_get_connection = transaction.get_connection


def _get_connection(using=None):
    if using is None:
        using = _migrating.get() or _current.get() or DEFAULT_DB_ALIAS
    return connections[using]


def install():
    """transaction.atomic(), on_commit() and friends with no `using` act on the
    active company's database, where its business rows are written."""
    if transaction.get_connection is not _get_connection:
        transaction.get_connection = _get_connection
    _connect_signals()


# ── copies of control rows ───────────────────────────────────────────────

def _fields(model):
    return list(model._meta.local_concrete_fields)


def insert_raw(model, objs, alias, batch=200):
    """INSERT the rows exactly as they are (keys, timestamps), no save() or signals."""
    fields = _fields(model)
    qs = model._base_manager.using(alias)
    objs = list(objs)
    for i in range(0, len(objs), batch):
        qs._insert(objs[i:i + batch], fields=fields, using=alias, raw=True)


def upsert(model, objs, alias):
    """Insert or overwrite the rows in `alias` by primary key, no save() or signals."""
    objs = list(objs)
    if not objs:
        return
    qs = model._base_manager.using(alias)
    pk = model._meta.pk
    existing = set()
    pks = [o.pk for o in objs]
    for i in range(0, len(pks), 500):
        existing.update(qs.filter(pk__in=pks[i:i + 500]).values_list("pk", flat=True))
    new = []
    for o in objs:
        if o.pk in existing:
            qs.filter(pk=o.pk).update(**{f.attname: getattr(o, f.attname)
                                         for f in _fields(model) if f is not pk})
        else:
            new.append(o)
    insert_raw(model, new, alias)


def _company_copy(company):
    from copy import copy
    c = copy(company)
    c.database_url_env = c.database_url_encrypted = ""   # the key stays in control
    return c


def _user_copy(user):
    from copy import copy
    u = copy(user)
    u.password = "!"            # signs in through the control database only
    return u


def sync_user(user, alias, force=False):
    stamp = getattr(user, "updated_at", None)
    key = (alias, "accounts.user", user.pk, stamp)
    if key in _synced and not force:
        return
    from apps.accounts.models import User
    upsert(User, [_user_copy(user)], alias)
    _synced.add(key)


def sync_company_rows(company, alias, force=False):
    """Copy the company row, its memberships and members into its database."""
    key = (alias, "organizations.company", company.pk, company.updated_at)
    if key in _synced and not force:
        return
    from apps.accounts.models import User
    from apps.organizations.models import Branch, Company, CompanyMembership, Department

    company = Company.objects.using(DEFAULT_DB_ALIAS).get(pk=company.pk)
    upsert(Company, [_company_copy(company)], alias)
    members = list(CompanyMembership.objects.using(DEFAULT_DB_ALIAS).filter(company=company))
    users = User.objects.using(DEFAULT_DB_ALIAS).filter(pk__in=[m.user_id for m in members])
    upsert(User, [_user_copy(u) for u in users], alias)
    _upsert_memberships(members, alias, Branch, Department, CompanyMembership)
    _synced.add(key)


def _upsert_memberships(members, alias, Branch, Department, CompanyMembership):
    branches = set(Branch._base_manager.using(alias).filter(
        pk__in=[m.branch_id for m in members if m.branch_id]).values_list("pk", flat=True))
    depts = set(Department._base_manager.using(alias).filter(
        pk__in=[m.department_id for m in members if m.department_id]).values_list("pk", flat=True))
    for m in members:
        if m.branch_id and m.branch_id not in branches:
            m.branch_id = None
        if m.department_id and m.department_id not in depts:
            m.department_id = None
    upsert(CompanyMembership, members, alias)


def _on_save(sender, instance, raw=False, using=None, **kwargs):
    if raw:
        return
    label = sender._meta.label_lower
    try:
        if using == DEFAULT_DB_ALIAS:
            _mirror_from_control(label, instance)
        elif is_company_alias(using) and label in BACK_MIRRORED:
            upsert(sender, [instance], DEFAULT_DB_ALIAS)
    except Exception:            # an unreachable company database must not stop the save
        log.exception("Could not copy %s %s to its company database", label, instance.pk)


def _mirror_from_control(label, instance):
    from apps.organizations.models import Branch, Company, CompanyMembership, Department
    if label == "organizations.company":
        if instance.own_database and company_database_url(instance):
            upsert(Company, [_company_copy(instance)], register(instance))
    elif label == "organizations.companymembership":
        company = Company.objects.using(DEFAULT_DB_ALIAS).filter(pk=instance.company_id, own_database=True).first()
        if company:
            alias = register(company)
            sync_user(instance.user, alias, force=True)
            _upsert_memberships([instance], alias, Branch, Department, CompanyMembership)
    elif label == "accounts.user":
        for company in Company.objects.using(DEFAULT_DB_ALIAS).filter(
                own_database=True, memberships__user=instance).distinct():
            sync_user(instance, register(company), force=True)


def _on_delete(sender, instance, using=None, **kwargs):
    if using != DEFAULT_DB_ALIAS:
        return
    try:
        from apps.organizations.models import Company
        company = Company.objects.using(DEFAULT_DB_ALIAS).filter(pk=instance.company_id, own_database=True).first()
        if company:
            sender._base_manager.using(register(company)).filter(pk=instance.pk)._raw_delete(register(company))
    except Exception:
        log.exception("Could not remove membership %s from its company database", instance.pk)


_signals_connected = False


def _connect_signals():
    global _signals_connected
    if _signals_connected:
        return
    from django.db.models.signals import post_delete, post_save

    from apps.accounts.models import User
    from apps.organizations.models import Branch, Company, CompanyMembership, Department
    for model in (User, Company, CompanyMembership, Branch, Department):
        post_save.connect(_on_save, sender=model, dispatch_uid=f"tenancy-save-{model._meta.label_lower}")
    post_delete.connect(_on_delete, sender=CompanyMembership, dispatch_uid="tenancy-delete-membership")
    _signals_connected = True


# ── finding a row when no one is signed in ───────────────────────────────

def find_anywhere(lookup):
    """For callbacks from payment gateways and the like, which arrive with a key
    but no signed-in company: `lookup()` is tried on the shared database, then
    on each company database. The database it is found in stays active for
    the rest of the request (the middleware switches back at the end)."""
    from apps.organizations.models import Company
    with using_alias(None):
        obj = lookup()
    owner = None
    if obj is not None:
        owner = Company.objects.using(DEFAULT_DB_ALIAS).filter(
            pk=getattr(obj, "company_id", None), own_database=True).first()
        if owner is None:
            _current.set(None)
            return obj          # a shared-database company: found where it lives
    companies = [owner] if owner else Company.objects.using(DEFAULT_DB_ALIAS).filter(own_database=True)
    for company in companies:
        try:
            token = _current.set(register(company))
        except Exception:
            log.exception("Company database for %s is not reachable", company)
            continue
        try:
            found = lookup()
        except Exception:
            log.exception("Lookup failed in the database of %s", company)
            found = None
        if found is not None:
            return found
        _current.reset(token)
    return None
