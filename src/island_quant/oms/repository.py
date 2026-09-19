"""SQLite ACID adapter for OMS order state, journal, fills and outbox."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from island_quant.oms.models import (
    OMSEvent,
    OMSInvariantError,
    OMSOrder,
    OMSState,
    validate_transition,
)


class OMSPersistenceError(RuntimeError):
    pass


class SQLiteOMSRepository:
    schema_version = 1

    def __init__(self, path: Path, *, environment: str = "paper", timeout: float = 1.0) -> None:
        if environment != "paper":
            raise ValueError("SQLite OMS adapter is restricted to paper environment")
        self.path = path
        self.environment = environment
        self.timeout = timeout

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > self.schema_version:
                raise OMSPersistenceError("paper OMS database schema is newer than this service")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    client_order_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    instrument_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    filled_quantity INTEGER NOT NULL DEFAULT 0,
                    limit_price TEXT,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    sequence INTEGER NOT NULL,
                    environment TEXT NOT NULL CHECK(environment = 'paper'),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL REFERENCES orders(order_id),
                    sequence INTEGER NOT NULL,
                    business_identity TEXT NOT NULL UNIQUE,
                    payload_checksum TEXT NOT NULL,
                    document TEXT NOT NULL,
                    UNIQUE(order_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    outbox_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL REFERENCES orders(order_id),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    action TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT
                );
                CREATE TABLE IF NOT EXISTS fills (
                    fill_id TEXT PRIMARY KEY,
                    business_identity TEXT NOT NULL UNIQUE,
                    order_id TEXT NOT NULL REFERENCES orders(order_id),
                    quantity INTEGER NOT NULL,
                    price TEXT NOT NULL,
                    event_time TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version',?)",
                (str(self.schema_version),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key,value) VALUES('environment','paper')"
            )
            connection.execute(f"PRAGMA user_version={self.schema_version}")
        self.integrity_check()

    def integrity_check(self) -> None:
        try:
            with self._connect() as connection:
                result = connection.execute("PRAGMA integrity_check").fetchone()
                environment = connection.execute(
                    "SELECT value FROM metadata WHERE key='environment'"
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise OMSPersistenceError("paper OMS database integrity check failed") from exc
        if result is None or result[0] != "ok" or environment is None or environment[0] != "paper":
            raise OMSPersistenceError("paper OMS database integrity or environment mismatch")

    def create(self, order: OMSOrder, event: OMSEvent) -> OMSOrder:
        if order.state is not OMSState.CREATED or event.state is not OMSState.CREATED:
            raise OMSInvariantError("new order must start in Created state")
        event.verify()
        try:
            with self._transaction() as connection:
                existing = connection.execute(
                    "SELECT * FROM orders WHERE idempotency_key=?", (order.idempotency_key,)
                ).fetchone()
                if existing is not None:
                    return _order(existing)
                connection.execute(
                    """INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    _order_values(order),
                )
                self._insert_event(connection, event)
        except sqlite3.IntegrityError as exc:
            raise OMSPersistenceError(
                "duplicate OMS identity conflicts with existing state"
            ) from exc
        except sqlite3.OperationalError as exc:
            raise OMSPersistenceError("paper OMS database is locked or unavailable") from exc
        return order

    def transition(
        self,
        order_id: str,
        event: OMSEvent,
        *,
        expected_version: int,
        filled_delta: int = 0,
        outbox: tuple[str, str, dict[str, object]] | None = None,
    ) -> OMSOrder:
        event.verify()
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM orders WHERE order_id=?", (order_id,)
                ).fetchone()
                if row is None:
                    raise OMSPersistenceError("OMS order not found")
                current = _order(row)
                duplicate = connection.execute(
                    "SELECT document FROM events WHERE event_id=?", (event.event_id,)
                ).fetchone()
                if duplicate is not None:
                    return current
                if current.version != expected_version:
                    raise OMSPersistenceError("optimistic concurrency conflict")
                if event.expected_previous_state is not current.state:
                    raise OMSInvariantError("event expected previous state mismatch")
                if event.sequence != current.sequence + 1:
                    raise OMSInvariantError("OMS event sequence gap")
                validate_transition(current.state, event.state)
                filled = current.filled_quantity + filled_delta
                if not 0 <= filled <= current.quantity:
                    raise OMSInvariantError("fill exceeds remaining order quantity")
                updated = OMSOrder(
                    current.order_id,
                    current.client_order_id,
                    current.idempotency_key,
                    current.instrument_id,
                    current.side,
                    current.quantity,
                    filled,
                    current.limit_price,
                    event.state,
                    current.version + 1,
                    event.sequence,
                    current.environment,
                    current.created_at,
                    event.received_time,
                )
                changed = connection.execute(
                    """UPDATE orders SET filled_quantity=?,state=?,version=?,sequence=?,updated_at=?
                       WHERE order_id=? AND version=?""",
                    (
                        filled,
                        event.state.value,
                        updated.version,
                        updated.sequence,
                        updated.updated_at.isoformat(),
                        order_id,
                        expected_version,
                    ),
                )
                if changed.rowcount != 1:
                    raise OMSPersistenceError("optimistic concurrency update failed")
                self._insert_event(connection, event)
                if outbox is not None:
                    outbox_id, action, payload = outbox
                    connection.execute(
                        """INSERT INTO outbox VALUES(?,?,?,?,?,'pending',0,?,NULL)""",
                        (
                            outbox_id,
                            order_id,
                            current.idempotency_key,
                            action,
                            json.dumps(payload, sort_keys=True, separators=(",", ":")),
                            event.received_time.isoformat(),
                        ),
                    )
                return updated
        except sqlite3.IntegrityError as exc:
            raise OMSPersistenceError("OMS event or outbox identity conflict") from exc
        except sqlite3.OperationalError as exc:
            raise OMSPersistenceError("paper OMS database is locked or unavailable") from exc

    def record_fill(
        self,
        order_id: str,
        fill_id: str,
        business_identity: str,
        quantity: int,
        price: Decimal,
        event_time: datetime,
    ) -> bool:
        try:
            with self._transaction() as connection:
                existing = connection.execute(
                    "SELECT quantity,price FROM fills WHERE business_identity=?",
                    (business_identity,),
                ).fetchone()
                if existing is not None:
                    if int(existing[0]) != quantity or Decimal(existing[1]) != price:
                        raise OMSPersistenceError("duplicate fill business identity conflicts")
                    return False
                connection.execute(
                    "INSERT INTO fills VALUES(?,?,?,?,?,?)",
                    (
                        fill_id,
                        business_identity,
                        order_id,
                        quantity,
                        str(price),
                        event_time.isoformat(),
                    ),
                )
                return True
        except sqlite3.IntegrityError as exc:
            raise OMSPersistenceError("duplicate fill identity conflicts") from exc

    def transition_fill(
        self,
        order_id: str,
        event: OMSEvent,
        *,
        expected_version: int,
        fill_id: str,
        business_identity: str,
        quantity: int,
        price: Decimal,
        event_time: datetime,
    ) -> OMSOrder:
        """Atomically persist a unique fill and its resulting order state."""
        event.verify()
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM orders WHERE order_id=?", (order_id,)
                ).fetchone()
                if row is None:
                    raise OMSPersistenceError("OMS order not found")
                current = _order(row)
                prior = connection.execute(
                    "SELECT quantity,price FROM fills WHERE business_identity=?",
                    (business_identity,),
                ).fetchone()
                if prior is not None:
                    if int(prior[0]) != quantity or Decimal(prior[1]) != price:
                        raise OMSPersistenceError("duplicate fill business identity conflicts")
                    return current
                if current.version != expected_version:
                    raise OMSPersistenceError("optimistic concurrency conflict")
                if event.expected_previous_state is not current.state:
                    raise OMSInvariantError("fill expected previous state mismatch")
                if event.sequence != current.sequence + 1:
                    raise OMSInvariantError("OMS event sequence gap")
                validate_transition(current.state, event.state)
                filled = current.filled_quantity + quantity
                if not 0 < filled <= current.quantity:
                    raise OMSInvariantError("fill exceeds remaining order quantity")
                updated = OMSOrder(
                    current.order_id,
                    current.client_order_id,
                    current.idempotency_key,
                    current.instrument_id,
                    current.side,
                    current.quantity,
                    filled,
                    current.limit_price,
                    event.state,
                    current.version + 1,
                    event.sequence,
                    current.environment,
                    current.created_at,
                    event.received_time,
                )
                connection.execute(
                    "INSERT INTO fills VALUES(?,?,?,?,?,?)",
                    (
                        fill_id,
                        business_identity,
                        order_id,
                        quantity,
                        str(price),
                        event_time.isoformat(),
                    ),
                )
                changed = connection.execute(
                    """UPDATE orders SET filled_quantity=?,state=?,version=?,sequence=?,updated_at=?
                       WHERE order_id=? AND version=?""",
                    (
                        filled,
                        event.state.value,
                        updated.version,
                        updated.sequence,
                        updated.updated_at.isoformat(),
                        order_id,
                        expected_version,
                    ),
                )
                if changed.rowcount != 1:
                    raise OMSPersistenceError("optimistic concurrency update failed")
                self._insert_event(connection, event)
                return updated
        except sqlite3.IntegrityError as exc:
            raise OMSPersistenceError("fill or event identity conflict") from exc
        except sqlite3.OperationalError as exc:
            raise OMSPersistenceError("paper OMS database is locked or unavailable") from exc

    def get(self, order_id: str) -> OMSOrder:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM orders WHERE order_id=?", (order_id,)
            ).fetchone()
        if row is None:
            raise KeyError(order_id)
        return _order(row)

    def by_idempotency_key(self, key: str) -> OMSOrder | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM orders WHERE idempotency_key=?", (key,)
            ).fetchone()
        return _order(row) if row is not None else None

    def list_orders(self) -> tuple[OMSOrder, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM orders ORDER BY created_at,order_id"
            ).fetchall()
        return tuple(_order(row) for row in rows)

    def events(self, order_id: str) -> tuple[OMSEvent, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT document FROM events WHERE order_id=? ORDER BY sequence", (order_id,)
            ).fetchall()
        return tuple(_event(json.loads(row[0])) for row in rows)

    def pending_outbox(self) -> tuple[dict[str, Any], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM outbox WHERE status='pending' ORDER BY created_at,outbox_id"
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def fills(self) -> tuple[dict[str, Any], ...]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM fills ORDER BY event_time,fill_id").fetchall()
        return tuple(dict(row) for row in rows)

    def outbox(self) -> tuple[dict[str, Any], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM outbox ORDER BY created_at,outbox_id"
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def mark_outbox_delivered(self, outbox_id: str, delivered_at: datetime) -> None:
        with self._transaction() as connection:
            connection.execute(
                """UPDATE outbox SET status='delivered', attempts=attempts+1, delivered_at=?
                   WHERE outbox_id=?""",
                (delivered_at.isoformat(), outbox_id),
            )

    def increment_outbox_attempt(self, outbox_id: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE outbox SET attempts=attempts+1 WHERE outbox_id=?", (outbox_id,)
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = self._open()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.timeout)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @staticmethod
    def _insert_event(connection: sqlite3.Connection, event: OMSEvent) -> None:
        document = json.dumps(_event_document(event), sort_keys=True, separators=(",", ":"))
        connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?)",
            (
                event.event_id,
                event.order_id,
                event.sequence,
                event.business_identity,
                event.payload_checksum,
                document,
            ),
        )


def _order_values(order: OMSOrder) -> tuple[object, ...]:
    return (
        order.order_id,
        order.client_order_id,
        order.idempotency_key,
        order.instrument_id,
        order.side,
        order.quantity,
        order.filled_quantity,
        str(order.limit_price) if order.limit_price is not None else None,
        order.state.value,
        order.version,
        order.sequence,
        order.environment,
        order.created_at.isoformat(),
        order.updated_at.isoformat(),
    )


def _order(row: sqlite3.Row) -> OMSOrder:
    return OMSOrder(
        str(row["order_id"]),
        str(row["client_order_id"]),
        str(row["idempotency_key"]),
        str(row["instrument_id"]),
        str(row["side"]),
        int(row["quantity"]),
        int(row["filled_quantity"]),
        Decimal(row["limit_price"]) if row["limit_price"] else None,
        OMSState(row["state"]),
        int(row["version"]),
        int(row["sequence"]),
        str(row["environment"]),
        datetime.fromisoformat(row["created_at"]),
        datetime.fromisoformat(row["updated_at"]),
    )


def _event_document(event: OMSEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "schema_version": event.schema_version,
        "order_id": event.order_id,
        "client_order_id": event.client_order_id,
        "idempotency_key": event.idempotency_key,
        "correlation_id": event.correlation_id,
        "causation_id": event.causation_id,
        "expected_previous_state": event.expected_previous_state.value
        if event.expected_previous_state
        else None,
        "state": event.state.value,
        "event_time": event.event_time.isoformat(),
        "received_time": event.received_time.isoformat(),
        "sequence": event.sequence,
        "reason_code": event.reason_code,
        "business_identity": event.business_identity,
        "payload": event.payload,
        "payload_checksum": event.payload_checksum,
    }


def _event(value: dict[str, Any]) -> OMSEvent:
    event = OMSEvent(
        str(value["event_id"]),
        int(value["schema_version"]),
        str(value["order_id"]),
        str(value["client_order_id"]),
        str(value["idempotency_key"]),
        str(value["correlation_id"]),
        str(value["causation_id"]) if value["causation_id"] else None,
        OMSState(value["expected_previous_state"]) if value["expected_previous_state"] else None,
        OMSState(value["state"]),
        datetime.fromisoformat(value["event_time"]),
        datetime.fromisoformat(value["received_time"]),
        int(value["sequence"]),
        str(value["reason_code"]),
        str(value["business_identity"]),
        tuple((str(k), str(v)) for k, v in value["payload"]),
        str(value["payload_checksum"]),
    )
    event.verify()
    return event
