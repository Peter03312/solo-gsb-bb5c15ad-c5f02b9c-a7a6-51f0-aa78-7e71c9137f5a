"""SQLite 持久化。所有变更由 service 层包在单个事务内提交，保证原子性。"""
from __future__ import annotations

import os
import sqlite3
import threading

from .models import Closure, Edge, Exit, Group, State

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (
    id                   INTEGER PRIMARY KEY,
    source               TEXT NOT NULL,
    target               TEXT NOT NULL,
    travel_minutes       INTEGER NOT NULL CHECK (travel_minutes >= 1),
    capacity_per_minute  INTEGER NOT NULL CHECK (capacity_per_minute >= 1)
);
CREATE TABLE IF NOT EXISTS exits (
    id   INTEGER PRIMARY KEY,
    node TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS closures (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_id       INTEGER NOT NULL REFERENCES edges(id),
    start_minute  INTEGER NOT NULL,
    end_minute    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS groups (
    id                  INTEGER PRIMARY KEY,
    people              INTEGER NOT NULL,
    source              TEXT NOT NULL,
    earliest_departure  INTEGER NOT NULL,
    priority            INTEGER NOT NULL,
    deadline            INTEGER NOT NULL,
    route               TEXT NOT NULL DEFAULT '[]',
    departure           INTEGER,
    arrival             INTEGER,
    status              TEXT NOT NULL DEFAULT 'unscheduled',
    reason              TEXT
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('now', '0')"
    )
    conn.commit()


# ---------------------------------------------------------------- 读取

def load_state(conn: sqlite3.Connection) -> State:
    edges = {
        r["id"]: Edge(r["id"], r["source"], r["target"],
                      r["travel_minutes"], r["capacity_per_minute"])
        for r in conn.execute("SELECT * FROM edges")
    }
    exits = {r["id"]: Exit(r["id"], r["node"]) for r in conn.execute("SELECT * FROM exits")}
    closures = {
        r["id"]: Closure(r["id"], r["edge_id"], r["start_minute"], r["end_minute"])
        for r in conn.execute("SELECT * FROM closures")
    }
    groups: dict[int, Group] = {}
    import json
    for r in conn.execute("SELECT * FROM groups"):
        groups[r["id"]] = Group(
            id=r["id"], people=r["people"], source=r["source"],
            earliest_departure=r["earliest_departure"], priority=r["priority"],
            deadline=r["deadline"], route=json.loads(r["route"]),
            departure=r["departure"], arrival=r["arrival"],
            status=r["status"], reason=r["reason"],
        )
    row = conn.execute("SELECT value FROM meta WHERE key='now'").fetchone()
    now = int(row["value"]) if row else 0
    return State(edges=edges, exits=exits, closures=closures, groups=groups, now=now)


# ---------------------------------------------------------------- 写入

def replace_state(
    conn: sqlite3.Connection,
    edges: list[Edge],
    exits: list[Exit],
    closures: list[Closure],
    groups: list[Group],
) -> None:
    """整库替换（场景重置）：时钟归 0。调用方负责事务。"""
    conn.executescript(
        "DELETE FROM groups; DELETE FROM closures; DELETE FROM exits; "
        "DELETE FROM edges;"
    )
    conn.executemany(
        "INSERT INTO edges(id, source, target, travel_minutes, capacity_per_minute) "
        "VALUES (?,?,?,?,?)",
        [(e.id, e.source, e.target, e.travel_minutes, e.capacity_per_minute) for e in edges],
    )
    conn.executemany(
        "INSERT INTO exits(id, node) VALUES (?,?)",
        [(x.id, x.node) for x in exits],
    )
    # 显式 id 的封闭记录保留原编号
    conn.executemany(
        "INSERT INTO closures(id, edge_id, start_minute, end_minute) "
        "VALUES (?,?,?,?)",
        [(c.id, c.edge_id, c.start_minute, c.end_minute) for c in closures],
    )
    conn.execute("DELETE FROM sqlite_sequence WHERE name='closures'")
    _upsert_groups(conn, groups)
    set_now(conn, 0)


def add_closures(conn: sqlite3.Connection, closures: list[Closure]) -> None:
    conn.executemany(
        "INSERT INTO closures(edge_id, start_minute, end_minute) VALUES (?,?,?)",
        [(c.edge_id, c.start_minute, c.end_minute) for c in closures],
    )


def delete_closure(conn: sqlite3.Connection, closure_id: int) -> bool:
    cur = conn.execute("DELETE FROM closures WHERE id=?", (closure_id,))
    return cur.rowcount > 0


def add_groups(conn: sqlite3.Connection, groups: list[Group]) -> None:
    _upsert_groups(conn, groups)


def delete_group(conn: sqlite3.Connection, group_id: int) -> bool:
    cur = conn.execute("DELETE FROM groups WHERE id=?", (group_id,))
    return cur.rowcount > 0


def _upsert_groups(conn: sqlite3.Connection, groups: list[Group]) -> None:
    import json
    for g in groups:
        conn.execute(
            "INSERT INTO groups(id, people, source, earliest_departure, priority, "
            "deadline, route, departure, arrival, status, reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "people=excluded.people, source=excluded.source, "
            "earliest_departure=excluded.earliest_departure, "
            "priority=excluded.priority, deadline=excluded.deadline, "
            "route=excluded.route, departure=excluded.departure, "
            "arrival=excluded.arrival, status=excluded.status, reason=excluded.reason",
            (g.id, g.people, g.source, g.earliest_departure, g.priority,
             g.deadline, json.dumps(g.route), g.departure, g.arrival,
             g.status, g.reason),
        )


def set_now(conn: sqlite3.Connection, now: int) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('now', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(now),),
    )
