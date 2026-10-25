"""TorrentHub 配置库：实例 + 设置（种子数据不落库，全部实时读）。"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("TH_DB_PATH", "/app/data/hub.db"))
_lock = threading.Lock()

# 首次启动不预置实例，避免把任何账号密码写进仓库；在「设置 → 实例」里添加即可。
DEFAULT_INSTANCES: list[tuple] = []


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with _lock, _conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS instances(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   name TEXT NOT NULL,
                   kind TEXT NOT NULL,
                   url TEXT NOT NULL,
                   username TEXT DEFAULT '',
                   password TEXT DEFAULT '',
                   enabled INTEGER DEFAULT 1,
                   webui TEXT DEFAULT '',
                   sort INTEGER DEFAULT 0,
                   created_at TEXT DEFAULT ''
               )"""
        )
        conn.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(instances)").fetchall()}
        if "webui" not in cols:                       # 老库平滑升级
            conn.execute("ALTER TABLE instances ADD COLUMN webui TEXT DEFAULT ''")
        if DEFAULT_INSTANCES and not conn.execute("SELECT 1 FROM instances LIMIT 1").fetchone():
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            conn.executemany(
                "INSERT INTO instances(name, kind, url, username, password, enabled, sort, created_at) VALUES(?,?,?,?,?,?,?,?)",
                [(n, k, u, user, pwd, en, i, now) for i, (n, k, u, user, pwd, en) in enumerate(DEFAULT_INSTANCES)],
            )


def init_stats() -> None:
    with _lock, _conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS samples(
                   ts INTEGER PRIMARY KEY, total INTEGER, seeding INTEGER, error INTEGER,
                   downloading INTEGER, dl_speed INTEGER, ul_speed INTEGER, free INTEGER)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS audit(
                   id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT,
                   action TEXT, target TEXT, detail TEXT, ok INTEGER)"""
        )


def add_sample(row: dict) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO samples(ts,total,seeding,error,downloading,dl_speed,ul_speed,free) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (int(row.get("ts") or time.time()), int(row.get("total") or 0), int(row.get("seeding") or 0),
             int(row.get("error") or 0), int(row.get("downloading") or 0),
             int(row.get("dl_speed") or 0), int(row.get("ul_speed") or 0), int(row.get("free") or 0)),
        )
        # 只留 7 天
        conn.execute("DELETE FROM samples WHERE ts < ?", (int(time.time()) - 7 * 86400,))


def list_samples(hours: int = 24) -> list[dict]:
    since = int(time.time() - max(1, hours) * 3600)
    with _lock, _conn() as conn:
        rows = conn.execute("SELECT * FROM samples WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    return [dict(r) for r in rows]


def add_audit(actor: str, action: str, target: str = "", detail: str = "", ok: bool = True) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO audit(ts, actor, action, target, detail, ok) VALUES(?,?,?,?,?,?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), actor or "web", action, target, detail[:500], 1 if ok else 0),
        )


def list_audit(limit: int = 100, q: str = "") -> list[dict]:
    sql = "SELECT * FROM audit"
    params: tuple = ()
    if q:
        sql += " WHERE action LIKE ? OR target LIKE ? OR detail LIKE ?"
        params = (f"%{q}%", f"%{q}%", f"%{q}%")
    sql += " ORDER BY id DESC LIMIT ?"
    with _lock, _conn() as conn:
        rows = conn.execute(sql, params + (min(max(1, limit), 500),)).fetchall()
    return [dict(r) for r in rows]


def list_instances(only_enabled: bool = False) -> list[dict]:
    sql = "SELECT * FROM instances"
    if only_enabled:
        sql += " WHERE enabled=1"
    sql += " ORDER BY sort, id"
    with _lock, _conn() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def get_instance(iid: int) -> dict | None:
    with _lock, _conn() as conn:
        row = conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
    return dict(row) if row else None


def save_instance(data: dict) -> dict:
    iid = data.get("id")
    fields = {
        "name": (data.get("name") or "").strip() or "未命名",
        "kind": (data.get("kind") or "qb").strip().lower(),
        "url": (data.get("url") or "").strip(),
        "username": data.get("username") or "",
        "password": data.get("password") or "",
        "enabled": 1 if data.get("enabled", True) else 0,
        "webui": (data.get("webui") or "").strip(),
    }
    if fields["kind"] not in ("qb", "tr"):
        fields["kind"] = "qb"
    with _lock, _conn() as conn:
        if iid:
            old = conn.execute("SELECT password FROM instances WHERE id=?", (iid,)).fetchone()
            if old and not fields["password"]:
                fields["password"] = old["password"]      # 不填密码表示不改
            conn.execute(
                "UPDATE instances SET name=?, kind=?, url=?, username=?, password=?, enabled=?, webui=? WHERE id=?",
                (fields["name"], fields["kind"], fields["url"], fields["username"],
                 fields["password"], fields["enabled"], fields["webui"], iid),
            )
        else:
            cur = conn.execute(
                "INSERT INTO instances(name, kind, url, username, password, enabled, webui, sort, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (fields["name"], fields["kind"], fields["url"], fields["username"],
                 fields["password"], fields["enabled"], fields["webui"], 99, time.strftime("%Y-%m-%d %H:%M:%S")),
            )
            iid = cur.lastrowid
    return get_instance(int(iid)) or {}


def delete_instance(iid: int) -> None:
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM instances WHERE id=?", (iid,))


def get_settings() -> dict:
    with _lock, _conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def save_settings(patch: dict) -> dict:
    with _lock, _conn() as conn:
        for key, value in patch.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, "" if value is None else str(value)),
            )
    return get_settings()
