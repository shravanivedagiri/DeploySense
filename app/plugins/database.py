"""Database plugin: connectivity, authentication, schema and pool errors."""
import re

from .base import Plugin

_ENGINES = [
    ("postgresql", re.compile(r"(?i)(postgres|psql|pg_hba|\bpg\b|:5432|sqlstate|prisma.*postgres)")),
    ("mysql", re.compile(r"(?i)(mysql|mariadb|:3306|access denied for user)")),
    ("mongodb", re.compile(r"(?i)(mongo|:27017)")),
    ("redis", re.compile(r"(?i)(redis|:6379)")),
]
_DEFAULT_PORT = {"postgresql": "5432", "mysql": "3306", "mongodb": "27017", "redis": "6379"}

# psql commands rewritten for other engines so the fix matches what the team actually runs
_MYSQL_SWAPS = [
    (re.compile(r"psql -h (\S+) -U (\S+) -l"), r"mysql -h \1 -u \2 -p -e 'SHOW DATABASES;'"),
    (re.compile(r"psql -h (\S+) -U (\S+) -c 'CREATE DATABASE ([^;']+);'"), r"mysql -h \1 -u \2 -p -e 'CREATE DATABASE \3;'"),
    (re.compile(r"psql -h (\S+) -U (\S+) -d (\S+) -c 'select 1'"), r"mysql -h \1 -u \2 -p \3 -e 'SELECT 1'"),
    (re.compile(r"--image=postgres:16 (.*?) -- psql -h (\S+) -U (\S+) -d (\S+) -c 'select 1'"),
     r"--image=mysql:8 \1 -- mysql -h \2 -u \3 -p \4 -e 'SELECT 1'"),
    (re.compile(r"psql -h (\S+) -U (\S+) -c 'show max_connections;' -c 'select count\(\*\) from pg_stat_activity;'"),
     r"mysql -h \1 -u \2 -p -e \"SHOW VARIABLES LIKE 'max_connections'; SHOW STATUS LIKE 'Threads_connected';\""),
]


class DatabasePlugin(Plugin):
    name = "database_plugin"
    label = "Database plugin"
    description = ("Diagnoses database connection refused or timed out, wrong credentials, missing database, "
                   "unapplied migrations, connection pool exhaustion and TLS requirements for PostgreSQL, "
                   "MySQL, MongoDB and Redis.")
    kb_plugin = "database"
    signals = re.compile(r"(?i)(postgres|psql|mysql|mongo|redis|database|\bdb\b|sql|:5432|:3306|:27017|:6379|"
                         r"migration|relation \"|no such table|pg_hba|connection pool|too many connections)")

    def enrich(self, parsed, values):
        engine = None
        text = "\n".join(e.raw for e in parsed["errors"] + parsed["warnings"])
        for name, rx in _ENGINES:
            if rx.search(text):
                engine = name
                break
        extra = {}
        if engine:
            extra["engine"] = engine
            if not values.get("port"):
                extra["port"] = _DEFAULT_PORT[engine]
        return {"values": extra, "details": {"engine": engine} if engine else {}}

    def adapt_command(self, command, values):
        if values.get("engine") == "mysql":
            for rx, repl in _MYSQL_SWAPS:
                command = rx.sub(repl, command)
        return command
