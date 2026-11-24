import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    scene_id TEXT NOT NULL DEFAULT 'sim_default',
    raw_input TEXT NOT NULL DEFAULT '',
    parser TEXT NOT NULL DEFAULT 'manual',
    pickup_location_id TEXT NOT NULL,
    deadline TEXT,
    deadline_kind TEXT NOT NULL DEFAULT 'none',
    status TEXT NOT NULL,
    plan_version INTEGER NOT NULL DEFAULT 0,
    pickup_status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stops (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    location_id TEXT NOT NULL,
    items TEXT NOT NULL DEFAULT '[]',
    priority TEXT NOT NULL DEFAULT 'normal',
    status TEXT NOT NULL DEFAULT 'pending',
    arrived_at TEXT,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_stops_task ON stops(task_id, seq);

CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    plan_version INTEGER NOT NULL,
    ordered_stops TEXT NOT NULL,
    route_node_ids TEXT NOT NULL,
    route_edge_ids TEXT NOT NULL,
    polyline TEXT NOT NULL,
    estimated_duration_seconds REAL NOT NULL DEFAULT 0,
    assumptions TEXT NOT NULL DEFAULT '[]',
    unreachable_stops TEXT NOT NULL DEFAULT '[]',
    warnings TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_plans_task_version ON plans(task_id, plan_version);

CREATE TABLE IF NOT EXISTS road_events (
    id TEXT PRIMARY KEY,
    scene_id TEXT NOT NULL DEFAULT 'sim_default',
    edge_id TEXT NOT NULL,
    edge_fraction REAL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    authority TEXT NOT NULL DEFAULT 'normal',
    confirmed_by TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT,
    observe_count INTEGER NOT NULL DEFAULT 0,
    last_observed_at TEXT
);

CREATE TABLE IF NOT EXISTS execution_state (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    mode TEXT NOT NULL,
    plan_version INTEGER NOT NULL DEFAULT 0,
    current_edge_id TEXT,
    edge_progress REAL NOT NULL DEFAULT 0,
    last_node_id TEXT,
    position_x REAL,
    position_y REAL,
    remaining_node_ids TEXT NOT NULL DEFAULT '[]',
    remaining_edge_ids TEXT NOT NULL DEFAULT '[]',
    dwell_remaining REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_log (
    request_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    action TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    plan_version INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, seq);

CREATE TABLE IF NOT EXISTS agent_traces (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    agent TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    action TEXT NOT NULL,
    input_summary TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    tools TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'success',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_traces_task ON agent_traces(task_id, created_at);

CREATE TABLE IF NOT EXISTS task_assignments (
    task_id TEXT PRIMARY KEY,
    assignee TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    accepted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_notes (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    author TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_notes_task ON task_notes(task_id, created_at);

CREATE TABLE IF NOT EXISTS semantic_events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    payload TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS semantic_obstacles (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    obstacle_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    active INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (task_id, obstacle_id)
);
CREATE TABLE IF NOT EXISTS sim_runtime (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 0,
    speed_mps REAL NOT NULL DEFAULT 40,
    hold_reason TEXT NOT NULL DEFAULT '',
    auto_resume INTEGER NOT NULL DEFAULT 0,
    emergency_hold INTEGER NOT NULL DEFAULT 0,
    decision_key TEXT NOT NULL DEFAULT '',
    waiting TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS upstream_simulations (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 0,
    category TEXT NOT NULL DEFAULT 'pedestrian',
    phase INTEGER NOT NULL DEFAULT 0,
    elapsed REAL NOT NULL DEFAULT 0,
    last_edge_id TEXT,
    next_generate_at REAL NOT NULL DEFAULT 0,
    generated_count INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS scenes (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'sim',
    virtual_seconds REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS obstacle_truth (
    scene_id TEXT NOT NULL,
    obstacle_id TEXT NOT NULL,
    edge_id TEXT NOT NULL,
    edge_fraction REAL,
    category TEXT NOT NULL,
    present INTEGER NOT NULL DEFAULT 1,
    clear_at_virtual REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scene_id, obstacle_id)
);

CREATE TABLE IF NOT EXISTS road_known (
    scene_id TEXT NOT NULL,
    obstacle_id TEXT NOT NULL,
    edge_id TEXT NOT NULL,
    edge_fraction REAL,
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    clear_evidence INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    authority TEXT NOT NULL DEFAULT 'normal',
    revision INTEGER NOT NULL DEFAULT 1,
    observed_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scene_id, obstacle_id)
);
CREATE INDEX IF NOT EXISTS idx_road_known_scene ON road_known(scene_id, active);
"""

# One authoritative writer per process. Nested services share the outer transaction,
# so ingestion, agent decisions and plan changes commit (or roll back) together.
operation_lock = threading.RLock()
_connections = threading.local()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    existing = getattr(_connections, "connection", None)
    if existing is not None:
        yield existing
        return
    conn = _connect()
    _connections.connection = conn
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _connections.connection = None
        conn.close()


def _add_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _add_missing(conn, "sim_runtime", "emergency_hold", "emergency_hold INTEGER NOT NULL DEFAULT 0")
        _add_missing(conn, "tasks", "scene_id", "scene_id TEXT NOT NULL DEFAULT 'sim_default'")
        _add_missing(conn, "road_events", "scene_id", "scene_id TEXT NOT NULL DEFAULT 'sim_default'")
        _add_missing(conn, "road_events", "edge_fraction", "edge_fraction REAL")
        _add_missing(conn, "road_events", "authority", "authority TEXT NOT NULL DEFAULT 'normal'")
        _add_missing(conn, "road_events", "observe_count", "observe_count INTEGER NOT NULL DEFAULT 0")
        _add_missing(conn, "road_events", "last_observed_at", "last_observed_at TEXT")
        _add_missing(conn, "road_known", "clear_evidence", "clear_evidence INTEGER NOT NULL DEFAULT 0")
        _add_missing(conn, "upstream_simulations", "last_edge_id", "last_edge_id TEXT")
        _add_missing(conn, "upstream_simulations", "next_generate_at", "next_generate_at REAL NOT NULL DEFAULT 0")
        _add_missing(conn, "upstream_simulations", "generated_count", "generated_count INTEGER NOT NULL DEFAULT 0")
        stamp = now_iso()
        conn.execute("INSERT OR IGNORE INTO scenes (id, kind, virtual_seconds, created_at, updated_at) VALUES (?, ?, 0, ?, ?)",
                     ("sim_default", "sim", stamp, stamp))
        conn.execute("INSERT OR IGNORE INTO scenes (id, kind, virtual_seconds, created_at, updated_at) VALUES (?, ?, 0, ?, ?)",
                     ("real_default", "real", stamp, stamp))
        conn.execute("UPDATE tasks SET scene_id = 'real_default' WHERE mode = 'real' AND scene_id = 'sim_default'")



def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
