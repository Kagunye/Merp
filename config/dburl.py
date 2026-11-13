"""Turn a database URL into a Django DATABASES entry.

Kept apart from the settings modules so the app can read a company's own
database URL at run time (apps.core.tenancy) the same way settings read
DATABASE_URL: sqlite:///..., postgres://... or mysql://user:pass@host:3306/db.
"""
import environ


def database_config(url):
    """The DATABASES entry for `url`.

    MySQL is driven by PyMySQL (pure Python, nothing to compile) and set up
    for this app: utf8mb4 so names and receipts keep every character, strict
    mode so a bad value is refused rather than silently cut, and READ
    COMMITTED like PostgreSQL. Needs MySQL 8.0+ or MariaDB 10.6+.
    """
    db = environ.Env.db_url_config(url)
    if "mysql" in db.get("ENGINE", ""):
        try:
            import pymysql
            pymysql.install_as_MySQLdb()
        except ImportError:      # mysqlclient installed instead: fine
            pass
        opts = db.setdefault("OPTIONS", {})
        opts.setdefault("charset", "utf8mb4")
        opts.setdefault("init_command", "SET sql_mode='STRICT_TRANS_TABLES', innodb_strict_mode=1")
        opts.setdefault("isolation_level", "read committed")
        db.setdefault("CONN_MAX_AGE", 60)
        db.setdefault("TEST", {}).setdefault("CHARSET", "utf8mb4")
        db["TEST"].setdefault("COLLATION", "utf8mb4_unicode_ci")
    return db
