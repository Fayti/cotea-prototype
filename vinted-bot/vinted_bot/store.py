"""Persistance SQLite : annonces vues, filigrane d'ID par recherche, historique de prix."""
from __future__ import annotations

import json
import sqlite3
import statistics
import time


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
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
            """
        )

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
        cur = self.db.execute(
            "INSERT OR IGNORE INTO seen(search, item_id, ts) VALUES(?, ?, ?)",
            (search, item_id, time.time()),
        )
        self.db.commit()
        return cur.rowcount == 1

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
        self.db.execute("INSERT INTO alerts(ts, payload) VALUES(?, ?)", (time.time(), json.dumps(payload)))
        self.db.commit()

    def recent_alerts(self, limit: int = 150) -> list[dict]:
        rows = self.db.execute("SELECT payload FROM alerts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def clear_alerts(self) -> None:
        self.db.execute("DELETE FROM alerts")
        self.db.commit()

    def prune(self, keep_days: float = 14) -> None:
        cutoff = time.time() - keep_days * 86400
        self.db.execute("DELETE FROM seen WHERE ts < ?", (cutoff,))
        self.db.execute("DELETE FROM prices WHERE ts < ?", (cutoff,))
        self.db.execute("DELETE FROM alerts WHERE id NOT IN (SELECT id FROM alerts ORDER BY id DESC LIMIT 500)")
        self.db.commit()

    def close(self) -> None:
        self.db.close()
