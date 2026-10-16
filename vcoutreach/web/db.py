"""Workspace SQLite connections and idempotent schema initialization."""
from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from vcoutreach.web.config import SCHEMA_PATH, get_workspace

def get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    workspace = get_workspace()
    path = workspace.path(db_path) if db_path is not None else workspace.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA journal_mode=DELETE')
    return conn

@contextmanager
def cursor_ctx(db_path: Path | None = None):
    conn = get_conn(db_path)
    try:
        yield conn, conn.cursor()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db(db_path: Path | None = None) -> None:
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding='utf-8'))
        conn.commit()
    finally:
        conn.close()

def apply_migrations(db_path: Path | None = None, migrations_dir: Path | None = None) -> list[str]:
    init_db(db_path)
    conn = get_conn(db_path)
    applied = []
    try:
        folder = migrations_dir or SCHEMA_PATH.parent / 'migrations'
        for path in sorted(Path(folder).glob('*.sql')):
            if conn.execute('SELECT 1 FROM schema_migrations WHERE name=?', (path.name,)).fetchone():
                continue
            conn.executescript(path.read_text(encoding='utf-8'))
            conn.execute('INSERT INTO schema_migrations(name) VALUES (?)', (path.name,))
            applied.append(path.name)
        conn.commit()
    finally:
        conn.close()
    return applied
