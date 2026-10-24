"""SQLite 状态库。

数据模型的关键一点：**商品事实**和**订阅关系**是两张表。

* ``items`` —— 一件商品只有一个事实：标题、当前价格、状态。
* ``subs``  —— 某个监控项「关注」了某件商品，以及它自己的价格基准、是否已提醒。

这样同一件商品可以同时被 L1（找新货）和 L2（盯价格）关注，
两边各自判断、互不干扰；L1 报过的「新上架」不会影响 L2 的价格基准。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from .models import Event, Item

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id    TEXT PRIMARY KEY,
    title      TEXT,
    price      INTEGER,
    status     TEXT,
    url        TEXT,
    thumb      TEXT,
    seller     TEXT,
    created    INTEGER,
    first_seen INTEGER,
    last_seen  INTEGER,
    price_min  INTEGER
);

CREATE TABLE IF NOT EXISTS subs (
    watch         TEXT NOT NULL,
    item_id       TEXT NOT NULL,
    first_seen    INTEGER,
    last_seen     INTEGER,
    last_price    INTEGER,
    below_alerted INTEGER DEFAULT 0,
    last_check    INTEGER DEFAULT 0,
    locked        INTEGER DEFAULT 0,
    PRIMARY KEY (watch, item_id)
);
CREATE INDEX IF NOT EXISTS idx_subs_watch ON subs(watch);
CREATE INDEX IF NOT EXISTS idx_subs_due ON subs(watch, last_check);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER,
    kind      TEXT,
    watch     TEXT,
    item_id   TEXT,
    title     TEXT,
    url       TEXT,
    price     INTEGER,
    old_price INTEGER,
    detail    TEXT,
    notified  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_notified ON events(notified);

CREATE TABLE IF NOT EXISTS watches (
    name          TEXT PRIMARY KEY,
    baseline_done INTEGER DEFAULT 0,
    last_run      INTEGER DEFAULT 0,
    total_seen    INTEGER DEFAULT 0
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()
        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def _migrate(self) -> None:
        """v1（一个商品只能属于一个监控项）升到 v2（商品与订阅分离）。"""
        rows = self.conn.execute("PRAGMA table_info(items)").fetchall()
        if not rows:
            return
        columns = {row[1] for row in rows}
        if "watch" in columns:
            log.warning(
                "检测到旧版数据库（商品表带 watch 列），重建商品表；"
                "旧的东西只是基线记录，重建后第一轮会自动重新建立基线"
            )
            self.conn.execute("DROP TABLE IF EXISTS items")
            self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass

    # -- watch -------------------------------------------------------------
    def watch_state(self, name: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM watches WHERE name = ?", (name,)).fetchone()
        if row is None:
            self.conn.execute("INSERT OR IGNORE INTO watches(name) VALUES (?)", (name,))
            self.conn.commit()
            return {"name": name, "baseline_done": 0, "last_run": 0, "total_seen": 0}
        return dict(row)

    def mark_baseline(self, name: str) -> None:
        self.conn.execute("UPDATE watches SET baseline_done = 1 WHERE name = ?", (name,))
        self.conn.commit()

    def touch_watch(self, name: str, seen: int) -> None:
        self.conn.execute(
            "UPDATE watches SET last_run = ?, total_seen = total_seen + ? WHERE name = ?",
            (int(time.time()), max(0, int(seen)), name),
        )
        self.conn.commit()

    # -- items（商品事实） --------------------------------------------------
    def get_item(self, item_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM items WHERE item_id = ?", (item_id,)).fetchone()
        return dict(row) if row else None

    def upsert_item(self, item: Item) -> None:
        now = int(time.time())
        existing = self.get_item(item.id)
        if existing is None:
            self.conn.execute(
                """INSERT INTO items(item_id, title, price, status, url, thumb, seller,
                                     created, first_seen, last_seen, price_min)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item.id,
                    item.name,
                    item.price,
                    item.status,
                    item.url,
                    item.thumb,
                    item.seller,
                    item.created,
                    now,
                    now,
                    item.price,
                ),
            )
        else:
            price_min = existing["price_min"]
            if item.price is not None:
                price_min = min(price_min, item.price) if price_min else item.price
            self.conn.execute(
                """UPDATE items
                      SET title=?, price=?, status=?, url=?, thumb=?, seller=?,
                          last_seen=?, price_min=?
                    WHERE item_id=?""",
                (
                    item.name or existing["title"],
                    item.price if item.price is not None else existing["price"],
                    item.status or existing["status"],
                    item.url or existing["url"],
                    item.thumb or existing["thumb"],
                    item.seller or existing["seller"],
                    now,
                    price_min,
                    item.id,
                ),
            )
        self.conn.commit()

    def set_item_status(self, item_id: str, status: str) -> None:
        self.conn.execute("UPDATE items SET status = ? WHERE item_id = ?", (status, item_id))
        self.conn.commit()

    # -- subs（订阅关系） ---------------------------------------------------
    def subscribe(self, watch: str, item_id: str, *, locked: bool = False) -> bool:
        """建立订阅。返回 True 表示这是第一次见到这个商品（即"新增"）。"""
        row = self.conn.execute(
            "SELECT 1 FROM subs WHERE watch = ? AND item_id = ?", (watch, item_id)
        ).fetchone()
        now = int(time.time())
        if row is None:
            item = self.get_item(item_id)
            self.conn.execute(
                """INSERT INTO subs(watch, item_id, first_seen, last_seen, last_price,
                                    below_alerted, last_check, locked)
                   VALUES (?,?,?,?,?,0,0,?)""",
                (watch, item_id, now, now, item["price"] if item else None, 1 if locked else 0),
            )
            self.conn.commit()
            return True
        self.conn.execute(
            "UPDATE subs SET last_seen = ?, locked = MAX(locked, ?) WHERE watch = ? AND item_id = ?",
            (now, 1 if locked else 0, watch, item_id),
        )
        self.conn.commit()
        return False

    def get_sub(self, watch: str, item_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM subs WHERE watch = ? AND item_id = ?", (watch, item_id)
        ).fetchone()
        return dict(row) if row else None

    def update_sub(self, watch: str, item_id: str, *, price: int | None, checked: bool = False) -> None:
        now = int(time.time())
        if checked:
            self.conn.execute(
                "UPDATE subs SET last_price = COALESCE(?, last_price), last_seen = ?, last_check = ?"
                " WHERE watch = ? AND item_id = ?",
                (price, now, now, watch, item_id),
            )
        else:
            self.conn.execute(
                "UPDATE subs SET last_price = COALESCE(?, last_price), last_seen = ?"
                " WHERE watch = ? AND item_id = ?",
                (price, now, watch, item_id),
            )
        self.conn.commit()

    def mark_below_alerted(self, watch: str, item_id: str) -> None:
        self.conn.execute(
            "UPDATE subs SET below_alerted = 1 WHERE watch = ? AND item_id = ?", (watch, item_id)
        )
        self.conn.commit()

    def locked_subs(self, watch: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT s.*, i.title, i.price, i.status, i.url, i.thumb
                 FROM subs s JOIN items i ON i.item_id = s.item_id
                WHERE s.watch = ? AND s.locked = 1
                ORDER BY s.first_seen ASC""",
            (watch,),
        ).fetchall()
        return [dict(r) for r in rows]

    def subs_due_for_status(self, watch: str, limit: int, interval: int) -> list[dict[str, Any]]:
        threshold = int(time.time()) - interval
        rows = self.conn.execute(
            """SELECT s.*, i.title, i.price, i.status, i.url
                 FROM subs s JOIN items i ON i.item_id = s.item_id
                WHERE s.watch = ? AND i.status = 'on_sale' AND s.last_check < ?
                ORDER BY s.last_check ASC
                LIMIT ?""",
            (watch, threshold, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def unlock(self, watch: str, item_id: str | None = None) -> int:
        if item_id:
            cur = self.conn.execute(
                "UPDATE subs SET locked = 0 WHERE watch = ? AND item_id = ?", (watch, item_id)
            )
        else:
            cur = self.conn.execute("UPDATE subs SET locked = 0 WHERE watch = ?", (watch,))
        self.conn.commit()
        return cur.rowcount

    def count_subs(self, watch: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM subs WHERE watch = ?", (watch,)
        ).fetchone()
        return int(row["n"]) if row else 0

    # -- events ------------------------------------------------------------
    def add_event(self, event: Event) -> None:
        self.conn.execute(
            """INSERT INTO events(ts, kind, watch, item_id, title, url, price, old_price, detail, notified)
               VALUES (?,?,?,?,?,?,?,?,?,0)""",
            (
                int(time.time()),
                event.kind,
                event.watch,
                event.item_id,
                event.title,
                event.url,
                event.price,
                event.old_price,
                event.detail,
            ),
        )
        self.conn.commit()

    def pending_events(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE notified = 0 ORDER BY id ASC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_events_notified(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self.conn.execute(f"UPDATE events SET notified = 1 WHERE id IN ({placeholders})", ids)
        self.conn.commit()

    def recent_events(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
