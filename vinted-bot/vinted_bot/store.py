"""Persistance SQLite : annonces vues, filigrane d'ID par recherche, historique de prix."""
from __future__ import annotations

import json
import sqlite3
import statistics
import time


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self._seen: dict[str, set[int]] = {}  # cache mémoire de la table seen
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen (
                search TEXT NOT NULL, item_id INTEGER NOT NULL, ts REAL NOT NULL,
                PRIMARY KEY (search, item_id));
            CREATE TABLE IF NOT EXISTS watermark (
                search TEXT PRIMARY KEY, max_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search TEXT NOT NULL, price REAL NOT NULL, ts REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS prices_search ON prices(search, id);
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tracked (
                item_id INTEGER PRIMARY KEY, search TEXT NOT NULL, url TEXT NOT NULL,
                alerted_ts REAL NOT NULL, status TEXT NOT NULL, status_ts REAL,
                handles TEXT NOT NULL DEFAULT '{}');
            """
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(alerts)")}
        if "item_id" not in columns:  # base créée par une version précédente
            self.db.execute("ALTER TABLE alerts ADD COLUMN item_id INTEGER")
            for rid, payload in self.db.execute("SELECT id, payload FROM alerts").fetchall():
                self.db.execute("UPDATE alerts SET item_id=? WHERE id=?", (json.loads(payload).get("id"), rid))
            self.db.commit()
        self.db.execute("CREATE INDEX IF NOT EXISTS alerts_item ON alerts(item_id)")

    def watermark(self, search: str) -> int | None:
        row = self.db.execute("SELECT max_id FROM watermark WHERE search=?", (search,)).fetchone()
        return row[0] if row else None

    def set_watermark(self, search: str, max_id: int) -> None:
        self.db.execute(
            "INSERT INTO watermark(search, max_id) VALUES(?, ?) "
            "ON CONFLICT(search) DO UPDATE SET max_id=max(max_id, excluded.max_id)",
            (search, max_id),
        )
        self.db.commit()

    def mark_seen(self, search: str, item_id: int) -> bool:
        """Renvoie True si l'annonce n'avait jamais été vue pour cette recherche."""
        seen = self._seen.get(search)
        if seen is None:
            rows = self.db.execute("SELECT item_id FROM seen WHERE search=?", (search,)).fetchall()
            seen = self._seen[search] = {r[0] for r in rows}
        if item_id in seen:
            return False
        seen.add(item_id)
        self.db.execute(
            "INSERT OR IGNORE INTO seen(search, item_id, ts) VALUES(?, ?, ?)",
            (search, item_id, time.time()),
        )
        self.db.commit()
        return True

    def add_price(self, search: str, price: float) -> None:
        self.db.execute(
            "INSERT INTO prices(search, price, ts) VALUES(?, ?, ?)", (search, price, time.time())
        )
        self.db.commit()

    def recent_prices(self, search: str, window: int) -> list[float]:
        rows = self.db.execute(
            "SELECT price FROM prices WHERE search=? ORDER BY id DESC LIMIT ?", (search, window)
        ).fetchall()
        return [r[0] for r in rows]

    def median_price(self, search: str, window: int, min_samples: int) -> float | None:
        prices = self.recent_prices(search, window)
        if len(prices) < min_samples:
            return None
        return statistics.median(prices)

    def add_alert(self, payload: dict) -> None:
        self.db.execute("INSERT INTO alerts(ts, payload, item_id) VALUES(?, ?, ?)",
                        (time.time(), json.dumps(payload), payload.get("id")))
        self.db.commit()

    def update_alert(self, item_id: int, patch: dict) -> dict | None:
        row = self.db.execute("SELECT id, payload FROM alerts WHERE item_id=? ORDER BY id DESC LIMIT 1",
                              (item_id,)).fetchone()
        if not row:
            return None
        payload = {**json.loads(row[1]), **patch}
        self.db.execute("UPDATE alerts SET payload=? WHERE id=?", (json.dumps(payload), row[0]))
        self.db.commit()
        return payload

    def save_tracked(self, t) -> None:
        self.db.execute(
            "INSERT INTO tracked(item_id, search, url, alerted_ts, status, status_ts, handles) "
            "VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT(item_id) DO UPDATE SET status=excluded.status, "
            "status_ts=excluded.status_ts, handles=excluded.handles",
            (t.item_id, t.search_key, t.url, t.alerted_ts, t.status, t.status_ts, json.dumps(t.handles)),
        )
        self.db.commit()

    def load_tracked(self, since: float) -> list[dict]:
        rows = self.db.execute(
            "SELECT item_id, search, url, alerted_ts, status, status_ts, handles FROM tracked WHERE alerted_ts >= ?",
            (since,)).fetchall()
        keys = ("item_id", "search", "url", "alerted_ts", "status", "status_ts", "handles")
        return [{**dict(zip(keys, r)), "handles": json.loads(r[6] or "{}")} for r in rows]

    def recent_alerts(self, limit: int = 150) -> list[dict]:
        rows = self.db.execute("SELECT payload FROM alerts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def clear_alerts(self) -> None:
        self.db.execute("DELETE FROM alerts")
        self.db.commit()

    def prune(self, keep_days: float = 14) -> None:
        cutoff = time.time() - keep_days * 86400
        self.db.execute("DELETE FROM seen WHERE ts < ?", (cutoff,))
        self._seen.clear()
        self.db.execute("DELETE FROM prices WHERE ts < ?", (cutoff,))
        self.db.execute("DELETE FROM alerts WHERE id NOT IN (SELECT id FROM alerts ORDER BY id DESC LIMIT 500)")
        self.db.execute("DELETE FROM tracked WHERE alerted_ts < ?", (cutoff,))
        self.db.commit()

    def close(self) -> None:
        self.db.close()
