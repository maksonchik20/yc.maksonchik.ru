#!/usr/bin/env python3
"""Copy an existing Django schema, including inactive-app tables, without signals.

Dependencies: PyMySQL and psycopg2. Python 3.8+. Credentials are read exclusively
from a mode-0600 JSON file. Neither credentials nor row values are logged.

Prepare a NEW, dedicated PostgreSQL database using the application's migrations
first. This utility intentionally refuses to invent schemas or omit tables.
All source tables must be InnoDB; all destination foreign keys must be deferrable.
The caller must stop every source writer before the final copy. A repeatable-read
snapshot makes rehearsal copies consistent but does NOT replace that write freeze.

Config format (supply actual values only in the private server-side file):
  {"mysql": {"host": "127.0.0.1", "database": "django", ...},
   "postgresql": {"host": "127.0.0.1", "dbname": "ghost_note", ...},
   "schema": "public", "source_datetime_timezone": "UTC"}

copy replaces ALL rows in the dedicated target database's configured schema.
It preserves source django_migrations/contenttypes/permissions/primary keys,
checks full normalized row contents, validates deferred constraints, and resets
sequences before committing. No ORM save, signal, payment or delivery code runs.
Memory is O(largest individual row + driver buffers), not O(database size).
"""

import argparse
import base64
import datetime
import decimal
import hashlib
import json
import math
import os
import stat
import struct
import sys
import time
import uuid


UTC = datetime.timezone.utc
GHOST_TEXT_PREFIX = "~ghostnote:text:b64:v1~"
TEXT_TYPES = {"text", "varchar", "bpchar", "name"}
INTEGER_TYPES = {"int2", "int4", "int8", "oid"}
SUPPORTED_TYPES = TEXT_TYPES | INTEGER_TYPES | {
    "bool", "bytea", "uuid", "json", "jsonb", "numeric", "float4", "float8",
    "date", "time", "timetz", "timestamp", "timestamptz", "interval",
}


def mysql_identifier(value):
    return "`" + value.replace("`", "``") + "`"


def pg_identifier(value):
    return '"' + value.replace('"', '""') + '"'


def qualified(schema, table):
    return pg_identifier(schema) + "." + pg_identifier(table)


def number_text(value):
    value = decimal.Decimal(value)
    if not value.is_finite():
        raise ValueError("Non-finite JSON or decimal number")
    # Decimal.normalize() may round under the current context; never use it for
    # financial values or arbitrary JSON numbers during a lossless migration.
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def json_node(value):
    """Typed canonical tree: object/array/string/number cannot collide."""
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, decimal.Decimal):
        return ["number", number_text(value)]
    if isinstance(value, str):
        return ["string", value]
    if isinstance(value, list):
        return ["array", [json_node(item) for item in value]]
    if isinstance(value, dict):
        return ["object", [[key, json_node(value[key])] for key in sorted(value)]]
    raise ValueError("Unsupported JSON scalar")


def json_load(value):
    def reject_constant(_value):
        raise ValueError("Non-finite JSON constant")
    return json.loads(value, parse_float=decimal.Decimal, parse_int=decimal.Decimal,
                      parse_constant=reject_constant)


def normalized(value, pg_type):
    """Return (PostgreSQL COPY input text, canonical full-value hash bytes)."""
    if value is None:
        return None, None
    if pg_type == "bytea":
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise ValueError("Binary value must not be decoded as text")
        raw = bytes(value)
        return "\\x" + raw.hex(), raw
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="strict")
    if pg_type in TEXT_TYPES:
        if not isinstance(value, str):
            raise ValueError("Text column contains a non-text value")
        text = value
    elif pg_type == "bool":
        if value not in (True, False, 0, 1):
            raise ValueError("Boolean column contains a value other than 0/1")
        text = "t" if bool(value) else "f"
    elif pg_type in INTEGER_TYPES:
        converted = int(value)
        if decimal.Decimal(str(value)) != converted:
            raise ValueError("Integer conversion would lose information")
        text = str(converted)
    elif pg_type == "uuid":
        text = str(uuid.UUID(str(value)))
    elif pg_type in ("json", "jsonb"):
        if not isinstance(value, str):
            raise ValueError("JSON must be read as raw database text")
        canonical = json.dumps(json_node(json_load(value)), ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8")
        if "\x00" in value:
            raise ValueError("NUL is not representable in PostgreSQL text")
        return value, canonical
    elif pg_type == "numeric":
        text = number_text(value)
    elif pg_type in ("float4", "float8"):
        value = float(value)
        if pg_type == "float4":
            value = struct.unpack("!f", struct.pack("!f", value))[0]
        text = repr(value)
        return text, ("nan" if math.isnan(value) else value.hex()).encode("ascii")
    elif pg_type in ("timestamp", "timestamptz"):
        if not isinstance(value, datetime.datetime):
            raise ValueError("Invalid or zero datetime in source")
        if pg_type == "timestamptz":
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            value = value.astimezone(UTC)
        elif value.tzinfo is not None:
            raise ValueError("Aware datetime cannot be silently made naive")
        text = value.isoformat(timespec="microseconds")
    elif pg_type == "date":
        if not isinstance(value, datetime.date) or isinstance(value, datetime.datetime):
            raise ValueError("Invalid or zero date in source")
        text = value.isoformat()
    elif pg_type in ("time", "timetz"):
        if isinstance(value, datetime.timedelta):
            if value.days != 0:
                raise ValueError("MySQL TIME is outside PostgreSQL time-of-day range")
            value = (datetime.datetime(2000, 1, 1) + value).time()
        if not isinstance(value, datetime.time):
            raise ValueError("Invalid time in source")
        text = value.isoformat(timespec="microseconds")
    elif pg_type == "interval":
        # Django DurationField stores integer microseconds on MySQL.
        if isinstance(value, datetime.timedelta):
            microseconds = ((value.days * 86400 + value.seconds) * 1000000
                            + value.microseconds)
        elif isinstance(value, int):
            microseconds = value
        else:
            raise ValueError("Unsupported duration representation")
        return str(microseconds) + " microseconds", str(microseconds).encode("ascii")
    else:
        raise ValueError("Unsupported PostgreSQL type")
    if "\x00" in text:
        raise ValueError("NUL is not representable in PostgreSQL text")
    return text, text.encode("utf-8")


def escape_copy(value):
    if value is None:
        return r"\N"
    return (value.replace("\\", "\\\\").replace("\t", "\\t")
            .replace("\n", "\\n").replace("\r", "\\r")
            .replace("\b", "\\b").replace("\f", "\\f").replace("\v", "\\v"))


class RowsDigest:
    def __init__(self, columns, from_postgresql=False):
        self.columns = columns
        self.from_postgresql = from_postgresql
        self.count = 0
        self.canonical_bytes = 0
        self.digest = hashlib.sha256()

    def add(self, row):
        if len(row) != len(self.columns):
            raise ValueError("Row width differs from validated schema")
        row_digest = hashlib.sha256()
        texts = []
        for column, value in zip(self.columns, row):
            try:
                if column.get("ghost_text_codec") and value is not None:
                    if not isinstance(value, str):
                        raise ValueError("Unexpected Ghost text representation")
                    if self.from_postgresql and value.startswith(GHOST_TEXT_PREFIX):
                        value = base64.b64decode(value[len(GHOST_TEXT_PREFIX):], validate=True).decode("utf-8")
                    raw = value.encode("utf-8")
                    text = value
                    if "\x00" in value or value.startswith(GHOST_TEXT_PREFIX):
                        text = GHOST_TEXT_PREFIX + base64.b64encode(raw).decode("ascii")
                else:
                    text, raw = normalized(value, column["type"])
            except (TypeError, ValueError, OverflowError, decimal.InvalidOperation):
                raise ValueError("Cannot losslessly convert column " + column["name"]) from None
            texts.append(text)
            if raw is None:
                row_digest.update(b"N")
            else:
                row_digest.update(b"V" + struct.pack("!Q", len(raw)))
                row_digest.update(raw)
                self.canonical_bytes += len(raw)
        self.digest.update(row_digest.digest())
        self.count += 1
        return texts

    def report(self):
        return {"rows": self.count, "sha256": self.digest.hexdigest(),
                "canonical_bytes": self.canonical_bytes}


class CopyStream:
    """psycopg2 file-like producer; only one source row is retained at a time."""
    def __init__(self, cursor, digest):
        self.cursor = cursor
        self.digest = digest
        self.pending = b""
        self.offset = 0
        self.exhausted = False

    def read(self, size=65536):
        if size < 0:
            raise ValueError("Unbounded COPY reads are not supported")
        chunks = []
        remaining = size
        while remaining:
            if self.offset == len(self.pending):
                row = self.cursor.fetchone()
                if row is None:
                    self.exhausted = True
                    break
                self.pending = ("\t".join(escape_copy(value)
                                           for value in self.digest.add(row)) + "\n").encode("utf-8")
                self.offset = 0
            amount = min(remaining, len(self.pending) - self.offset)
            chunks.append(self.pending[self.offset:self.offset + amount])
            self.offset += amount
            remaining -= amount
        return b"".join(chunks)


def load_config(path):
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("Credentials config must be a private regular file (0600)")
    with open(path, "r", encoding="utf-8") as source:
        config = json.load(source)
    if config.get("source_datetime_timezone") != "UTC":
        raise ValueError("Explicit source_datetime_timezone=UTC is required (Django USE_TZ)")
    return config


def connect(config):
    import pymysql
    import psycopg2
    mysql_config = dict(config["mysql"])
    mysql_config.setdefault("charset", "utf8mb4")
    mysql_config.setdefault("connect_timeout", 10)
    mysql_config.setdefault("read_timeout", 300)
    mysql_config["autocommit"] = False
    source = pymysql.connect(**mysql_config)
    target = psycopg2.connect(**config["postgresql"])
    target.set_client_encoding("UTF8")
    return source, target


def begin(source, target, writable=False):
    with source.cursor() as cursor:
        cursor.execute("SET SESSION time_zone = '+00:00'")
        cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
    target.set_session(isolation_level="REPEATABLE READ", readonly=not writable)
    with target.cursor() as cursor:
        cursor.execute("SET LOCAL TIME ZONE 'UTC'")
        cursor.execute("SET LOCAL lock_timeout = '10s'")
        cursor.execute("SET LOCAL statement_timeout = '15min'")


def inspect_schema(source, target, schema):
    with source.cursor() as cursor:
        cursor.execute("SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES "
                       "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME")
        tableToEngine = dict(cursor.fetchall())
        cursor.execute("SELECT TABLE_NAME FROM information_schema.VIEWS WHERE TABLE_SCHEMA=DATABASE()")
        if cursor.fetchone():
            raise ValueError("Source has views; migrate their definitions explicitly first")
    if not tableToEngine or any(engine != "InnoDB" for engine in tableToEngine.values()):
        raise ValueError("Every source table must use InnoDB for a consistent snapshot")
    with target.cursor() as cursor:
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s ORDER BY tablename", (schema,))
        target_tables = {row[0] for row in cursor.fetchall()}
        if set(tableToEngine) != target_tables:
            raise ValueError("Table mismatch: missing in target={} extra in target={}".format(
                sorted(set(tableToEngine) - target_tables), sorted(target_tables - set(tableToEngine))))
        cursor.execute("SELECT c.relname, con.conname FROM pg_constraint con "
                       "JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
                       "WHERE n.nspname=%s AND con.contype='f' AND NOT con.condeferrable", (schema,))
        if cursor.fetchall():
            raise ValueError("Non-deferrable target FK exists; do not disable integrity checks")
    tableToDefinition = {}
    for table in tableToEngine:
        with source.cursor() as cursor:
            cursor.execute("SELECT COLUMN_NAME, DATA_TYPE, EXTRA FROM information_schema.COLUMNS "
                           "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION", (table,))
            nameToSourceColumn = {row[0]: {"type": row[1], "extra": row[2]} for row in cursor.fetchall()}
            cursor.execute("SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
                           "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND CONSTRAINT_NAME='PRIMARY' "
                           "ORDER BY ORDINAL_POSITION", (table,))
            source_pk = [row[0] for row in cursor.fetchall()]
        with target.cursor() as cursor:
            cursor.execute("SELECT a.attname, t.typname, pg_get_serial_sequence(%s,a.attname) "
                           "FROM pg_attribute a JOIN pg_type t ON t.oid=a.atttypid "
                           "WHERE a.attrelid=%s::regclass AND a.attnum>0 AND NOT a.attisdropped "
                           "ORDER BY a.attnum", (qualified(schema, table), qualified(schema, table)))
            columns = [{"name": row[0], "type": row[1], "sequence": row[2]} for row in cursor.fetchall()]
            cursor.execute("SELECT a.attname FROM pg_index i "
                           "JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum,position) ON TRUE "
                           "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum "
                           "WHERE i.indrelid=%s::regclass AND i.indisprimary ORDER BY k.position",
                           (qualified(schema, table),))
            target_pk = [row[0] for row in cursor.fetchall()]
        if set(nameToSourceColumn) != {column["name"] for column in columns}:
            raise ValueError("Column mismatch for table " + table)
        if not source_pk or source_pk != target_pk:
            raise ValueError("A matching primary key is required for table " + table)
        for column in columns:
            if table == "ghost_note_ghosttextmessage" and column["name"] == "text":
                column["ghost_text_codec"] = True
            if column["type"] not in SUPPORTED_TYPES:
                raise ValueError("Unsupported column type: {}.{} {}".format(table, column["name"], column["type"]))
            if "auto_increment" in nameToSourceColumn[column["name"]]["extra"] and not column["sequence"]:
                raise ValueError("Target is missing auto-increment sequence: " + table + "." + column["name"])
        tableToDefinition[table] = {"columns": columns, "pk": source_pk}
    return tableToDefinition


def source_query(table, definition):
    column_names = ",".join(mysql_identifier(column["name"]) for column in definition["columns"])
    nameToType = {column["name"]: column["type"] for column in definition["columns"]}
    order = []
    for name in definition["pk"]:
        expression = mysql_identifier(name)
        if nameToType[name] in TEXT_TYPES:
            expression = "BINARY " + expression
        elif nameToType[name] == "uuid":
            expression = "LOWER(REPLACE(" + expression + ", '-', ''))"
        order.append(expression)
    return "SELECT " + column_names + " FROM " + mysql_identifier(table) + " ORDER BY " + ",".join(order)


def target_query(schema, table, definition):
    expressions = []
    nameToType = {column["name"]: column["type"] for column in definition["columns"]}
    for column in definition["columns"]:
        expression = pg_identifier(column["name"])
        if column["type"] in ("json", "jsonb"):
            expression += "::text"
        expressions.append(expression)
    order = [pg_identifier(name) + (' COLLATE "C"' if nameToType[name] in TEXT_TYPES else "")
             for name in definition["pk"]]
    return "SELECT " + ",".join(expressions) + " FROM " + qualified(schema, table) + " ORDER BY " + ",".join(order)


def read_source_digest(source, table, definition):
    import pymysql
    digest = RowsDigest(definition["columns"])
    with source.cursor(pymysql.cursors.SSCursor) as cursor:
        cursor.execute(source_query(table, definition))
        for row in cursor:
            digest.add(row)
    return digest.report()


def read_target_digest(target, schema, table, definition):
    digest = RowsDigest(definition["columns"], from_postgresql=True)
    with target.cursor(name="verify_" + uuid.uuid4().hex) as cursor:
        # Never buffer hundreds of screenshots together. Small legacy telemetry
        # rows can be fetched in batches to avoid millions of round trips.
        cursor.itersize = 1 if any(column["type"] == "bytea" for column in definition["columns"]) else 256
        cursor.execute(target_query(schema, table, definition))
        for row in cursor:
            digest.add(row)
    return digest.report()


def reset_sequences(target, schema, tableToDefinition):
    reset = 0
    with target.cursor() as cursor:
        for table, definition in tableToDefinition.items():
            for column in definition["columns"]:
                sequence = column["sequence"]
                if not sequence:
                    continue
                cursor.execute("SELECT seqstart,seqincrement FROM pg_sequence WHERE seqrelid=%s::regclass", (sequence,))
                start, increment = cursor.fetchone()
                if increment != 1:
                    raise ValueError("Unexpected sequence increment")
                cursor.execute("SELECT MAX(" + pg_identifier(column["name"]) + ") FROM " + qualified(schema, table))
                maximum = cursor.fetchone()[0]
                cursor.execute("SELECT setval(%s::regclass,%s,%s)",
                               (sequence, max(start, maximum) if maximum is not None else start,
                                maximum is not None and maximum >= start))
                reset += 1
    return reset


def copy_all(source, target, schema, tableToDefinition):
    import pymysql
    with target.cursor() as cursor:
        # No CASCADE: abort if anything outside this explicitly enumerated schema
        # references it. This must never touch WhoUpdate's separate database.
        cursor.execute("SET CONSTRAINTS ALL DEFERRED")
        cursor.execute("TRUNCATE TABLE " + ",".join(qualified(schema, table)
                                                    for table in tableToDefinition) + " RESTART IDENTITY")
    tableToDigest = {}
    for table, definition in tableToDefinition.items():
        digest = RowsDigest(definition["columns"])
        with source.cursor(pymysql.cursors.SSCursor) as source_cursor:
            source_cursor.execute(source_query(table, definition))
            stream = CopyStream(source_cursor, digest)
            with target.cursor() as target_cursor:
                statement = "COPY " + qualified(schema, table) + " (" + ",".join(
                    pg_identifier(column["name"]) for column in definition["columns"]) + ") FROM STDIN WITH (FORMAT text)"
                target_cursor.copy_expert(statement, stream, size=65536)
            if not stream.exhausted:
                raise ValueError("COPY did not consume source completely: " + table)
        tableToDigest[table] = digest.report()
        emit({"stage": "copied", "table": table, **digest.report()})
    return tableToDigest


def emit(value):
    print(json.dumps(value, sort_keys=True), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--mode", choices=("inspect", "copy", "verify"), default="inspect")
    parser.add_argument("--confirm-replace-target-database")
    parser.add_argument("--source-writers-frozen", action="store_true",
                        help="Required for production final copy; does not stop writers itself")
    parser.add_argument("--rehearsal-snapshot", action="store_true",
                        help="Explicitly allow a live-source repeatable-read rehearsal")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    expected_target = config["postgresql"].get("dbname") or config["postgresql"].get("database")
    if args.mode == "copy":
        if not isinstance(expected_target, str) or not expected_target.startswith("ghost"):
            parser.error("Dedicated migration target database name must start with ghost")
        if not expected_target or args.confirm_replace_target_database != expected_target:
            parser.error("copy requires the exact --confirm-replace-target-database name")
        if not args.source_writers_frozen and not args.rehearsal_snapshot:
            parser.error("copy requires --source-writers-frozen or --rehearsal-snapshot")
    schema = config.get("schema", "public")
    source = target = None
    started = time.monotonic()
    try:
        source, target = connect(config)
        begin(source, target, writable=args.mode == "copy")
        with target.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            actual_target = cursor.fetchone()[0]
        if actual_target != expected_target:
            raise ValueError("Connected target database does not match configured name")
        tableToDefinition = inspect_schema(source, target, schema)
        emit({"stage": "schema_validated", "tables": len(tableToDefinition),
              "target_database": actual_target, "mode": args.mode})
        if args.mode == "inspect":
            emit({"stage": "schema", "tables": tableToDefinition})
            return 0
        tableToDigest = copy_all(source, target, schema, tableToDefinition) if args.mode == "copy" else {}
        for table, definition in tableToDefinition.items():
            expected = tableToDigest.get(table) or read_source_digest(source, table, definition)
            actual = read_target_digest(target, schema, table, definition)
            if actual != expected:
                raise ValueError("Full-value verification mismatch for table " + table)
            emit({"stage": "verified", "table": table, **actual})
        if args.mode == "copy":
            with target.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                cursor.execute("SELECT count(*) FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
                               "JOIN pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname=%s AND NOT c.convalidated",
                               (schema,))
                if cursor.fetchone()[0]:
                    raise ValueError("Unvalidated target constraint remains")
            reset = reset_sequences(target, schema, tableToDefinition)
            target.commit()
            emit({"stage": "committed", "sequences_reset": reset, "tables": len(tableToDefinition),
                  "rows": sum(item["rows"] for item in tableToDigest.values()),
                  "seconds": round(time.monotonic() - started, 2)})
        else:
            emit({"stage": "verified_all", "tables": len(tableToDefinition),
                  "seconds": round(time.monotonic() - started, 2)})
        return 0
    except Exception as error:
        # Driver exception messages may embed SQL values or credentials. Report
        # safe validation text only; retain raw diagnostics privately if needed.
        safe_message = str(error) if isinstance(error, ValueError) else "Database operation failed; target transaction rolled back"
        emit({"stage": "failed", "error_type": type(error).__name__, "message": safe_message})
        return 1
    finally:
        if target is not None:
            target.rollback()
            target.close()
        if source is not None:
            source.rollback()
            source.close()


if __name__ == "__main__":
    sys.exit(main())
