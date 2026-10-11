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
"""

import asyncio
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
        document_json      MEDIUMTEXT  NOT NULL,
        PRIMARY KEY (doc_id),
        KEY idx_cp_plugged (charge_point_id, plugged_at),
        KEY idx_ended (ended_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
]

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
            finally:
                conn.close()

        try:
            await self._run(_op)
            if not self._ready:
                logging.info("Aurora history store ready (%s/%s)", AURORA_HOST, AURORA_DATABASE)
            self._ready = True
            self._disabled_reason = None
            return True
        except Exception as err:  # noqa: BLE001 - degrade gracefully
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
                        row[0]: {name: (float(row[i + 1] or 0) if name != "samples" else int(row[i + 1] or 0))
                                 for i, name in enumerate(_HOURLY_FIELDS)}
                        for row in cur.fetchall()
                    }

                    cur.execute(
                        "SELECT bucket_key, " + ", ".join(_DAILY_FIELDS) + " FROM daily_energy"
                    )
                    daily = {
                        row[0]: {name: (float(row[i + 1] or 0) if name != "samples" else int(row[i + 1] or 0))
                                 for i, name in enumerate(_DAILY_FIELDS)}
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

                    cur.execute("SELECT doc_id, document_json FROM charge_sessions")
                    sessions = {}
                    for row in cur.fetchall():
                        try:
                            sessions[row[0]] = json.loads(row[1])
                        except (TypeError, ValueError):
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
        if not self.enabled:
            return False
        if not self._ready and not await self.ensure_schema():
            return False

        def _op():
            conn = self._connect_sync()
            try:
                now = _utc_now()
                with conn.cursor() as cur:
                    hourly_rows = []
                    for key, bucket in hourly_buckets.items():
                        start, offset = _parse_iso(key)
                        hourly_rows.append((
                            key, start, offset,
                            float(bucket.get("sum_kw", 0) or 0),
                            float(bucket.get("sum_pv_kw", 0) or 0),
                            float(bucket.get("sum_grid_export_kw", 0) or 0),
                            float(bucket.get("sum_grid_import_kw", 0) or 0),
                            float(bucket.get("sum_load_kw", 0) or 0),
                            int(bucket.get("samples", 0) or 0),
                            now,
                        ))
                    if hourly_rows:
                        cur.executemany(
                            "INSERT INTO hourly_power (bucket_key, bucket_start, zone_offset, "
                            "sum_kw, sum_pv_kw, sum_grid_export_kw, sum_grid_import_kw, sum_load_kw, "
                            "samples, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                            "ON DUPLICATE KEY UPDATE bucket_start=VALUES(bucket_start), "
                            "zone_offset=VALUES(zone_offset), sum_kw=VALUES(sum_kw), "
                            "sum_pv_kw=VALUES(sum_pv_kw), sum_grid_export_kw=VALUES(sum_grid_export_kw), "
                            "sum_grid_import_kw=VALUES(sum_grid_import_kw), sum_load_kw=VALUES(sum_load_kw), "
                            "samples=VALUES(samples), updated_at=VALUES(updated_at)",
                            hourly_rows,
                        )

                    daily_rows = []
                    for key, bucket in daily_buckets.items():
                        daily_rows.append((
                            key,
                            float(bucket.get("import_kwh", 0) or 0),
                            float(bucket.get("export_kwh", 0) or 0),
                            float(bucket.get("load_kwh", 0) or 0),
                            float(bucket.get("net_kwh", 0) or 0),
                            float(bucket.get("cost", 0) or 0),
                            int(bucket.get("samples", 0) or 0),
                            now,
                        ))
                    if daily_rows:
                        cur.executemany(
                            "INSERT INTO daily_energy (bucket_key, import_kwh, export_kwh, load_kwh, "
                            "net_kwh, cost, samples, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                            "ON DUPLICATE KEY UPDATE import_kwh=VALUES(import_kwh), "
                            "export_kwh=VALUES(export_kwh), load_kwh=VALUES(load_kwh), "
                            "net_kwh=VALUES(net_kwh), cost=VALUES(cost), samples=VALUES(samples), "
                            "updated_at=VALUES(updated_at)",
                            daily_rows,
                        )
            finally:
                conn.close()

        try:
            await self._run(_op)
            self._ready = True
            self._disabled_reason = None
            return True
        except Exception as err:  # noqa: BLE001
            self._ready = False
            self._disabled_reason = str(err)
            logging.warning("Aurora graph history flush failed: %s", err)
            return False

    # ── Events ────────────────────────────────────────────────────────

    async def append_event(self, event, max_events):
        """Append one OCPP event and prune the table to ``max_events``."""
        if not self.enabled:
            return False
        if not self._ready and not await self.ensure_schema():
            return False

        def _op():
            details = event.get("details")
            conn = self._connect_sync()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO ocpp_events (event_time, charge_point_id, event_type, summary, details_json) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (
                            event.get("time", ""),
                            event.get("charge_point_id") or "",
                            event.get("type") or "",
                            event.get("summary") or "",
                            json.dumps(details) if details else None,
                        ),
                    )
                    cur.execute(
                        "DELETE FROM ocpp_events WHERE id NOT IN "
                        "(SELECT id FROM (SELECT id FROM ocpp_events ORDER BY id DESC LIMIT %s) keep)",
                        (int(max_events),),
                    )
            finally:
                conn.close()

        try:
            await self._run(_op)
            self._ready = True
            return True
        except Exception as err:  # noqa: BLE001
            self._ready = False
            self._disabled_reason = str(err)
            logging.warning("Aurora event append failed: %s", err)
            return False

    # ── Charge sessions ───────────────────────────────────────────────

    async def save_session(self, session):
        """Upsert one charge session document (full JSON + indexed columns)."""
        if not self.enabled:
            return False
        doc_id = session.get("_id")
        if not doc_id:
            return False
        if not self._ready and not await self.ensure_schema():
            return False

        def _op():
            conn = self._connect_sync()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO charge_sessions (doc_id, session_id, charge_point_id, connector_id, "
                        "state, health, plugged_at, ended_at, transaction_id, energy_delivered_wh, "
                        "updated_at, document_json) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE session_id=VALUES(session_id), "
                        "charge_point_id=VALUES(charge_point_id), connector_id=VALUES(connector_id), "
                        "state=VALUES(state), health=VALUES(health), plugged_at=VALUES(plugged_at), "
                        "ended_at=VALUES(ended_at), transaction_id=VALUES(transaction_id), "
                        "energy_delivered_wh=VALUES(energy_delivered_wh), updated_at=VALUES(updated_at), "
                        "document_json=VALUES(document_json)",
                        (
                            doc_id,
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
                            json.dumps(session, default=str),
                        ),
                    )
            finally:
                conn.close()

        try:
            await self._run(_op)
            self._ready = True
            return True
        except Exception as err:  # noqa: BLE001
            self._ready = False
            self._disabled_reason = str(err)
            logging.warning("Aurora charge session save failed: %s", err)
            return False

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
