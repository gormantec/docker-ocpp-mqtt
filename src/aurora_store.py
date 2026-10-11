"""aurora_store.py — Aurora (MySQL/MariaDB) history store for the OCPP bridge.

Split of responsibilities:

* **DocumentDB** keeps singleton + configuration state: per charge point
  ``{cp_id}:schedule`` config, ``cpstate:{cp_id}`` last-known connector state and
  meter readings, and ``site:metrics`` last-known site telemetry.
* **Aurora** keeps all historic/time-series data: hourly graph buckets, daily
  energy & cost history, recent OCPP events, and charge sessions (with their
  meter samples, faults and monitoring gaps).

The database is the same Aurora (Synology MariaDB) backend docker-iot exposes via
``/api/mobile/sql``. This service is granted a least-privilege MySQL user scoped
to a single database through an IAM ``AuroraAccess`` block — see
``docker-iot/src/auroraAclEngine.mjs``. Credentials arrive as ``AURORA_HOST`` /
``AURORA_PORT`` / ``AURORA_USERNAME`` / ``AURORA_PASSWORD`` / ``AURORA_DATABASE``.

Keys are stored in the same string form the bridge already uses in memory
(ISO-8601 hour keys, ``YYYY-MM-DD`` day keys, ``charge:{cp}:{session}`` document
ids) so data round-trips exactly while remaining queryable with SQL. Charge
sessions are stored as a full JSON document alongside indexed columns, because
their sample/fault/gap arrays are deeply nested.

Connections are opened per operation rather than pooled, so the one-time legacy
migration deliberately does all of its work on a single connection to avoid a
burst of connection attempts against the NAS MariaDB.
"""

import asyncio
import gzip
import json
import logging
import os
from datetime import datetime, timezone

try:
    import pymysql
except ImportError:  # pragma: no cover - declared in requirements.txt
    pymysql = None


AURORA_HOST = (os.environ.get("AURORA_HOST") or "").strip()
AURORA_PORT = int(os.environ.get("AURORA_PORT") or 3306)
AURORA_USER = (os.environ.get("AURORA_USERNAME") or os.environ.get("AURORA_USER") or "").strip()
AURORA_PASSWORD = os.environ.get("AURORA_PASSWORD", "")
AURORA_DATABASE = (os.environ.get("AURORA_DATABASE") or "ocpp_mqtt").strip()

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS hourly_power (
        bucket_key        VARCHAR(40) NOT NULL,
        bucket_start      DATETIME    NULL,
        zone_offset       VARCHAR(10) NOT NULL DEFAULT '',
        sum_kw            DOUBLE      NOT NULL DEFAULT 0,
        sum_pv_kw         DOUBLE      NOT NULL DEFAULT 0,
        sum_grid_export_kw DOUBLE     NOT NULL DEFAULT 0,
        sum_grid_import_kw DOUBLE     NOT NULL DEFAULT 0,
        sum_load_kw       DOUBLE      NOT NULL DEFAULT 0,
        samples           INT         NOT NULL DEFAULT 0,
        updated_at        DATETIME    NOT NULL,
        PRIMARY KEY (bucket_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_energy (
        bucket_key VARCHAR(16) NOT NULL,
        import_kwh DOUBLE      NOT NULL DEFAULT 0,
        export_kwh DOUBLE      NOT NULL DEFAULT 0,
        load_kwh   DOUBLE      NOT NULL DEFAULT 0,
        net_kwh    DOUBLE      NOT NULL DEFAULT 0,
        cost       DOUBLE      NOT NULL DEFAULT 0,
        samples    INT         NOT NULL DEFAULT 0,
        updated_at DATETIME    NOT NULL,
        PRIMARY KEY (bucket_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS ocpp_events (
        id               BIGINT      NOT NULL AUTO_INCREMENT,
        event_time       VARCHAR(40) NOT NULL,
        charge_point_id  VARCHAR(64) NOT NULL DEFAULT '',
        event_type       VARCHAR(64) NOT NULL DEFAULT '',
        summary          VARCHAR(600) NOT NULL DEFAULT '',
        details_json     MEDIUMTEXT  NULL,
        PRIMARY KEY (id),
        KEY idx_event_time (event_time)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS charge_sessions (
        doc_id             VARCHAR(96) NOT NULL,
        session_id         VARCHAR(40) NOT NULL,
        charge_point_id    VARCHAR(64) NOT NULL DEFAULT '',
        connector_id       INT         NULL,
        state              VARCHAR(32) NULL,
        health             VARCHAR(32) NULL,
        plugged_at         VARCHAR(40) NULL,
        ended_at           VARCHAR(40) NULL,
        transaction_id     BIGINT      NULL,
        energy_delivered_wh DOUBLE     NOT NULL DEFAULT 0,
        updated_at         VARCHAR(40) NULL,
        document_json      MEDIUMTEXT  NULL,
        document_gz        MEDIUMBLOB  NULL,
        PRIMARY KEY (doc_id),
        KEY idx_cp_plugged (charge_point_id, plugged_at),
        KEY idx_ended (ended_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
]

_HOURLY_UPSERT_SQL = (
    "INSERT INTO hourly_power (bucket_key, bucket_start, zone_offset, "
    "sum_kw, sum_pv_kw, sum_grid_export_kw, sum_grid_import_kw, sum_load_kw, "
    "samples, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE bucket_start=VALUES(bucket_start), "
    "zone_offset=VALUES(zone_offset), sum_kw=VALUES(sum_kw), "
    "sum_pv_kw=VALUES(sum_pv_kw), sum_grid_export_kw=VALUES(sum_grid_export_kw), "
    "sum_grid_import_kw=VALUES(sum_grid_import_kw), sum_load_kw=VALUES(sum_load_kw), "
    "samples=VALUES(samples), updated_at=VALUES(updated_at)"
)

_DAILY_UPSERT_SQL = (
    "INSERT INTO daily_energy (bucket_key, import_kwh, export_kwh, load_kwh, "
    "net_kwh, cost, samples, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE import_kwh=VALUES(import_kwh), "
    "export_kwh=VALUES(export_kwh), load_kwh=VALUES(load_kwh), "
    "net_kwh=VALUES(net_kwh), cost=VALUES(cost), samples=VALUES(samples), "
    "updated_at=VALUES(updated_at)"
)

_SESSION_UPSERT_SQL = (
    "INSERT INTO charge_sessions (doc_id, session_id, charge_point_id, connector_id, "
    "state, health, plugged_at, ended_at, transaction_id, energy_delivered_wh, "
    "updated_at, document_json, document_gz) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE session_id=VALUES(session_id), "
    "charge_point_id=VALUES(charge_point_id), connector_id=VALUES(connector_id), "
    "state=VALUES(state), health=VALUES(health), plugged_at=VALUES(plugged_at), "
    "ended_at=VALUES(ended_at), transaction_id=VALUES(transaction_id), "
    "energy_delivered_wh=VALUES(energy_delivered_wh), updated_at=VALUES(updated_at), "
    "document_json='', document_gz=VALUES(document_gz)"
)

_EVENT_INSERT_SQL = (
    "INSERT INTO ocpp_events (event_time, charge_point_id, event_type, summary, details_json) "
    "VALUES (%s,%s,%s,%s,%s)"
)

_EVENT_PRUNE_SQL = (
    "DELETE FROM ocpp_events WHERE id NOT IN "
    "(SELECT id FROM (SELECT id FROM ocpp_events ORDER BY id DESC LIMIT %s) keep)"
)

_HOURLY_FIELDS = (
    "sum_kw", "sum_pv_kw", "sum_grid_export_kw", "sum_grid_import_kw", "sum_load_kw", "samples",
)
_DAILY_FIELDS = ("import_kwh", "export_kwh", "load_kwh", "net_kwh", "cost", "samples")


def _parse_iso(value):
    if not isinstance(value, str) or not value:
        return None, ""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None, ""
    offset = ""
    if parsed.tzinfo is not None:
        offset = parsed.strftime("%z")
        parsed = parsed.replace(tzinfo=None)
    return parsed, offset


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Row builders (shared by the steady-state writers and the migration) ──

def _hourly_rows(hourly_buckets, now):
    rows = []
    for key, bucket in hourly_buckets.items():
        start, offset = _parse_iso(key)
        rows.append((
            key, start, offset,
            float(bucket.get("sum_kw", 0) or 0),
            float(bucket.get("sum_pv_kw", 0) or 0),
            float(bucket.get("sum_grid_export_kw", 0) or 0),
            float(bucket.get("sum_grid_import_kw", 0) or 0),
            float(bucket.get("sum_load_kw", 0) or 0),
            int(bucket.get("samples", 0) or 0),
            now,
        ))
    return rows


def _daily_rows(daily_buckets, now):
    rows = []
    for key, bucket in daily_buckets.items():
        rows.append((
            key,
            float(bucket.get("import_kwh", 0) or 0),
            float(bucket.get("export_kwh", 0) or 0),
            float(bucket.get("load_kwh", 0) or 0),
            float(bucket.get("net_kwh", 0) or 0),
            float(bucket.get("cost", 0) or 0),
            int(bucket.get("samples", 0) or 0),
            now,
        ))
    return rows


def _session_row(session):
    """Session row with the document gzip-compressed.

    A session can carry up to ``MAX_SESSION_SAMPLES`` meter samples (~1 MB of
    JSON). Sending that as plain text overflows the NAS MariaDB's 1 MiB
    ``max_allowed_packet`` once pymysql escapes it, so it is stored compressed;
    the indexed columns keep the data queryable with SQL.
    """
    raw = json.dumps(session, default=str).encode("utf-8")
    return (
        session.get("_id"),
        str(session.get("session_id") or ""),
        str(session.get("charge_point_id") or ""),
        int(session["connector_id"]) if session.get("connector_id") is not None else None,
        session.get("state"),
        session.get("health"),
        session.get("plugged_at"),
        session.get("ended_at"),
        int(session["transaction_id"]) if session.get("transaction_id") is not None else None,
        float(session.get("energy_delivered_wh", 0) or 0),
        session.get("updated_at"),
        '',
        gzip.compress(raw),
    )


def _event_rows(events):
    rows = []
    for event in events:
        details = event.get("details")
        rows.append((
            event.get("time", ""),
            event.get("charge_point_id") or "",
            event.get("type") or "",
            event.get("summary") or "",
            json.dumps(details) if details else None,
        ))
    return rows


def _pending_events(cur, events):
    """Events not already present, keyed by (time, charge point, type)."""
    cur.execute("SELECT event_time, charge_point_id, event_type FROM ocpp_events")
    existing = {(row[0], row[1], row[2]) for row in cur.fetchall()}
    return [
        event for event in events
        if (event.get("time", ""), event.get("charge_point_id") or "",
            event.get("type") or "") not in existing
    ]


def _ensure_column(cur, table, column, ddl):
    """Add ``column`` to ``table`` when it does not already exist."""
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s AND column_name=%s",
        (AURORA_DATABASE, table, column),
    )
    if cur.fetchone()[0] == 0:
        cur.execute(f"ALTER TABLE `{table}` ADD COLUMN {ddl}")


class AuroraHistoryStore:
    """Historic-data store for the OCPP bridge, backed by Aurora."""

    def __init__(self):
        self._ready = False
        self._lock = asyncio.Lock()
        self._disabled_reason = None

    @property
    def enabled(self):
        if pymysql is None:
            return False
        return bool(AURORA_HOST and AURORA_USER and AURORA_DATABASE)

    @property
    def status(self):
        if not self.enabled:
            return "disabled"
        return "ready" if self._ready else "unavailable"

    @property
    def database(self):
        return AURORA_DATABASE

    # ── Connection helpers ────────────────────────────────────────────

    def _connect_sync(self, with_database=True):
        params = dict(
            host=AURORA_HOST,
            port=AURORA_PORT,
            user=AURORA_USER,
            password=AURORA_PASSWORD,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=8,
        )
        if with_database:
            params["database"] = AURORA_DATABASE
        return pymysql.connect(**params)

    async def _run(self, operation):
        async with self._lock:
            return await asyncio.to_thread(operation)

    async def _execute(self, operation, what):
        """Run ``operation(cursor)`` on one fresh connection, mapping failures."""
        if not self.enabled:
            return False
        if not self._ready and not await self.ensure_schema():
            return False

        def _op():
            conn = self._connect_sync()
            try:
                with conn.cursor() as cur:
                    operation(cur)
            finally:
                conn.close()

        try:
            await self._run(_op)
            self._ready = True
            self._disabled_reason = None
            return True
        except Exception as err:  # noqa: BLE001 - degrade gracefully
            self._ready = False
            self._disabled_reason = str(err)
            logging.warning("Aurora %s failed: %s", what, err)
            return False

    async def ensure_schema(self):
        """Create the history tables (and database if our grant permits)."""
        if not self.enabled:
            return False

        def _op():
            try:
                conn = self._connect_sync(with_database=True)
            except pymysql.err.OperationalError as err:
                if err.args and err.args[0] == 1049:
                    bootstrap = self._connect_sync(with_database=False)
                    try:
                        with bootstrap.cursor() as cur:
                            cur.execute(
                                f"CREATE DATABASE IF NOT EXISTS `{AURORA_DATABASE}` "
                                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                            )
                    finally:
                        bootstrap.close()
                    conn = self._connect_sync(with_database=True)
                else:
                    raise
            try:
                with conn.cursor() as cur:
                    for statement in _SCHEMA_STATEMENTS:
                        cur.execute(statement)
                    # Tables created before session documents were compressed
                    # gain the column here rather than needing a manual ALTER.
                    _ensure_column(cur, "charge_sessions", "document_gz", "document_gz MEDIUMBLOB NULL")
            finally:
                conn.close()

        try:
            await self._run(_op)
            if not self._ready:
                logging.info("Aurora history store ready (%s/%s)", AURORA_HOST, AURORA_DATABASE)
            self._ready = True
            self._disabled_reason = None
            return True
        except Exception as err:  # noqa: BLE001
            self._ready = False
            self._disabled_reason = str(err)
            logging.warning("Aurora history store unavailable: %s", err)
            return False

    # ── Load ──────────────────────────────────────────────────────────

    async def load_history(self, max_events):
        """Return ``(hourly, daily, events, sessions)``; empty when unavailable."""
        empty = ({}, {}, [], {})
        if not self.enabled:
            return empty
        if not self._ready and not await self.ensure_schema():
            return empty

        def _op():
            conn = self._connect_sync()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT bucket_key, " + ", ".join(_HOURLY_FIELDS) + " FROM hourly_power"
                    )
                    hourly = {
                        row[0]: {
                            name: (int(row[i + 1] or 0) if name == "samples" else float(row[i + 1] or 0))
                            for i, name in enumerate(_HOURLY_FIELDS)
                        }
                        for row in cur.fetchall()
                    }

                    cur.execute(
                        "SELECT bucket_key, " + ", ".join(_DAILY_FIELDS) + " FROM daily_energy"
                    )
                    daily = {
                        row[0]: {
                            name: (int(row[i + 1] or 0) if name == "samples" else float(row[i + 1] or 0))
                            for i, name in enumerate(_DAILY_FIELDS)
                        }
                        for row in cur.fetchall()
                    }

                    cur.execute(
                        "SELECT event_time, charge_point_id, event_type, summary, details_json "
                        "FROM ocpp_events ORDER BY id DESC LIMIT %s",
                        (int(max_events),),
                    )
                    events = []
                    for row in reversed(cur.fetchall()):
                        event = {
                            "time": row[0],
                            "charge_point_id": row[1],
                            "type": row[2],
                            "summary": row[3],
                        }
                        if row[4]:
                            try:
                                event["details"] = json.loads(row[4])
                            except (TypeError, ValueError):
                                pass
                        events.append(event)

                    cur.execute("SELECT doc_id, document_json, document_gz FROM charge_sessions")
                    sessions = {}
                    for row in cur.fetchall():
                        try:
                            if row[2]:
                                sessions[row[0]] = json.loads(gzip.decompress(row[2]).decode("utf-8"))
                            elif row[1]:
                                sessions[row[0]] = json.loads(row[1])
                        except (TypeError, ValueError, OSError):
                            continue
                return hourly, daily, events, sessions
            finally:
                conn.close()

        try:
            result = await self._run(_op)
            self._ready = True
            return result
        except Exception as err:  # noqa: BLE001
            self._ready = False
            self._disabled_reason = str(err)
            logging.warning("Aurora history load failed: %s", err)
            return empty

    # ── Graph history (hourly + daily) ────────────────────────────────

    async def flush_graph_history(self, hourly_buckets, daily_buckets):
        """Upsert the supplied hourly and daily buckets. Returns True on success."""
        def _op(cur):
            now = _utc_now()
            hourly_rows = _hourly_rows(hourly_buckets, now)
            if hourly_rows:
                cur.executemany(_HOURLY_UPSERT_SQL, hourly_rows)
            daily_rows = _daily_rows(daily_buckets, now)
            if daily_rows:
                cur.executemany(_DAILY_UPSERT_SQL, daily_rows)

        return await self._execute(_op, "graph history flush")

    # ── Events ────────────────────────────────────────────────────────

    async def append_event(self, event, max_events):
        """Append one OCPP event and prune the table to ``max_events``."""
        return await self.append_events([event], max_events)

    async def append_events(self, events, max_events):
        """Bulk-append OCPP events and prune the table to ``max_events``."""
        if not events:
            return False

        def _op(cur):
            cur.executemany(_EVENT_INSERT_SQL, _event_rows(events))
            cur.execute(_EVENT_PRUNE_SQL, (int(max_events),))

        return await self._execute(_op, "event append")

    async def backfill_events(self, events, max_events):
        """Insert legacy events that are not already present (idempotent)."""
        if not events:
            return True

        def _op(cur):
            pending = _pending_events(cur, events)
            if pending:
                cur.executemany(_EVENT_INSERT_SQL, _event_rows(pending))
            cur.execute(_EVENT_PRUNE_SQL, (int(max_events),))

        return await self._execute(_op, "event backfill")

    # ── Charge sessions ───────────────────────────────────────────────

    async def save_session(self, session):
        """Upsert one charge session document (full JSON + indexed columns)."""
        if not session.get("_id"):
            return False

        def _op(cur):
            cur.execute(_SESSION_UPSERT_SQL, _session_row(session))

        return await self._execute(_op, "charge session save")

    # ── One-time legacy migration ─────────────────────────────────────

    async def migrate_legacy_history(self, hourly, daily, events, sessions, max_events):
        """Idempotent move of legacy DocumentDB history into Aurora.

        All work happens on a single connection so migrating a large legacy
        dataset does not open a burst of connections against the NAS MariaDB.
        Buckets and sessions are upserted; events are inserted only when not
        already present, so re-running after a partial failure is safe.

        Charge session documents are inserted one statement at a time: a session
        can hold up to ``MAX_SESSION_SAMPLES`` meter samples (~1 MB of JSON), and
        batching several of them into a single ``executemany`` packet would
        exceed the server's ``max_allowed_packet`` and drop the connection.
        """
        def _op(cur):
            now = _utc_now()
            hourly_rows = _hourly_rows(hourly, now)
            if hourly_rows:
                cur.executemany(_HOURLY_UPSERT_SQL, hourly_rows)
            daily_rows = _daily_rows(daily, now)
            if daily_rows:
                cur.executemany(_DAILY_UPSERT_SQL, daily_rows)
            for document in sessions.values():
                if document.get("_id"):
                    cur.execute(_SESSION_UPSERT_SQL, _session_row(document))
            if events:
                pending = _pending_events(cur, events)
                if pending:
                    cur.executemany(_EVENT_INSERT_SQL, _event_rows(pending))
                cur.execute(_EVENT_PRUNE_SQL, (int(max_events),))

        return await self._execute(_op, "legacy history migration")

    # ── Deletion ──────────────────────────────────────────────────────

    async def delete_documents(self, doc_ids):
        """Delete history rows/documents by their DocumentDB-style ids.

        Returns the number of rows deleted, or ``None`` when the store could not
        be reached (so callers keep the ids queued for a later retry).
        """
        ids = [str(doc_id) for doc_id in doc_ids]
        if not self.enabled or not ids:
            return None
        if not self._ready and not await self.ensure_schema():
            return None

        def _op():
            hourly = [i[len("history:hourly:"):] for i in ids if i.startswith("history:hourly:")]
            daily = [i[len("history:chargerdaily:"):] for i in ids if i.startswith("history:chargerdaily:")]
            sessions = [i for i in ids if i.startswith("charge:")]
            deleted = 0
            conn = self._connect_sync()
            try:
                with conn.cursor() as cur:
                    if hourly:
                        deleted += cur.executemany(
                            "DELETE FROM hourly_power WHERE bucket_key=%s", [(k,) for k in hourly]
                        ) or 0
                    if daily:
                        deleted += cur.executemany(
                            "DELETE FROM daily_energy WHERE bucket_key=%s", [(k,) for k in daily]
                        ) or 0
                    if sessions:
                        deleted += cur.executemany(
                            "DELETE FROM charge_sessions WHERE doc_id=%s", [(i,) for i in sessions]
                        ) or 0
            finally:
                conn.close()
            return deleted

        try:
            result = await self._run(_op)
            self._ready = True
            return result
        except Exception as err:  # noqa: BLE001
            self._ready = False
            self._disabled_reason = str(err)
            logging.warning("Aurora history delete failed: %s", err)
            return None
