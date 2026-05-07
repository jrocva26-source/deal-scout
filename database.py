"""
Deal Database - SQLite storage for deal history, watchlist, and dedup.
"""

import aiosqlite
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("deal_scout.db")

DB_PATH = "deal_scout.db"


class DealDatabase:
    """Async SQLite database for deal tracking."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.db: Optional[aiosqlite.Connection] = None

    async def initialize(self):
        """Create tables if they don't exist."""
        self.db = await aiosqlite.connect(self.db_path)
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS seen_deals (
                message_id TEXT PRIMARY KEY,
                channel_id TEXT,
                channel_name TEXT,
                product_name TEXT,
                sale_price REAL,
                msrp REAL,
                percent_off REAL,
                sku TEXT,
                retailer TEXT,
                mapper_url TEXT,
                score INTEGER,
                tier TEXT,
                alerted INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT,
                product_name TEXT,
                mapper_url TEXT,
                sku TEXT,
                retailer TEXT,
                last_price REAL,
                last_stock INTEGER DEFAULT 0,
                last_checked TIMESTAMP,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                active INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS alert_cooldowns (
                sku_key TEXT PRIMARY KEY,
                last_alerted TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_seen_sku ON seen_deals(sku);
            CREATE INDEX IF NOT EXISTS idx_seen_created ON seen_deals(created_at);
            CREATE INDEX IF NOT EXISTS idx_watchlist_active ON watchlist(active);
        """)
        await self.db.commit()
        logger.info("Database initialized")

    async def is_duplicate(self, message_id: str, sku: str = None, cooldown_minutes: int = 120) -> bool:
        """Check if we've already seen/alerted this deal recently."""
        # Exact message ID check
        cursor = await self.db.execute(
            "SELECT 1 FROM seen_deals WHERE message_id = ?", (message_id,)
        )
        if await cursor.fetchone():
            return True

        # SKU-based cooldown check
        if sku:
            cursor = await self.db.execute(
                "SELECT last_alerted FROM alert_cooldowns WHERE sku_key = ?",
                (sku,)
            )
            row = await cursor.fetchone()
            if row:
                last_alerted = datetime.fromisoformat(row[0])
                if datetime.now() - last_alerted < timedelta(minutes=cooldown_minutes):
                    return True

        return False

    async def record_deal(self, deal_data: dict):
        """Record a deal we've processed."""
        await self.db.execute("""
            INSERT OR REPLACE INTO seen_deals
            (message_id, channel_id, channel_name, product_name, sale_price,
             msrp, percent_off, sku, retailer, mapper_url, score, tier, alerted)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            deal_data.get("message_id"),
            deal_data.get("channel_id"),
            deal_data.get("channel_name"),
            deal_data.get("product_name"),
            deal_data.get("sale_price"),
            deal_data.get("msrp"),
            deal_data.get("percent_off"),
            deal_data.get("sku"),
            deal_data.get("retailer"),
            deal_data.get("mapper_url"),
            deal_data.get("score", 0),
            deal_data.get("tier", "skip"),
            deal_data.get("alerted", 0),
        ))
        await self.db.commit()

    async def record_alert(self, sku: str):
        """Record that we alerted on this SKU."""
        await self.db.execute("""
            INSERT OR REPLACE INTO alert_cooldowns (sku_key, last_alerted)
            VALUES (?, ?)
        """, (sku, datetime.now().isoformat()))
        await self.db.commit()

    async def add_to_watchlist(self, deal_data: dict, expire_days: int = 7):
        """Add a deal to the watchlist for periodic re-checking."""
        expires_at = datetime.now() + timedelta(days=expire_days)
        await self.db.execute("""
            INSERT INTO watchlist
            (message_id, product_name, mapper_url, sku, retailer,
             last_price, last_checked, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            deal_data.get("message_id"),
            deal_data.get("product_name"),
            deal_data.get("mapper_url"),
            deal_data.get("sku"),
            deal_data.get("retailer"),
            deal_data.get("sale_price"),
            datetime.now().isoformat(),
            expires_at.isoformat(),
        ))
        await self.db.commit()

    async def get_active_watchlist(self) -> list:
        """Get all active watchlist items."""
        cursor = await self.db.execute("""
            SELECT id, product_name, mapper_url, sku, retailer,
                   last_price, last_stock, last_checked
            FROM watchlist
            WHERE active = 1 AND expires_at > ?
        """, (datetime.now().isoformat(),))
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0], "product_name": r[1], "mapper_url": r[2],
                "sku": r[3], "retailer": r[4], "last_price": r[5],
                "last_stock": r[6], "last_checked": r[7],
            }
            for r in rows
        ]

    async def update_watchlist_item(self, item_id: int, price: float = None, stock: int = None):
        """Update a watchlist item with latest data."""
        updates = ["last_checked = ?"]
        params = [datetime.now().isoformat()]
        if price is not None:
            updates.append("last_price = ?")
            params.append(price)
        if stock is not None:
            updates.append("last_stock = ?")
            params.append(stock)
        params.append(item_id)

        await self.db.execute(
            f"UPDATE watchlist SET {', '.join(updates)} WHERE id = ?",
            params
        )
        await self.db.commit()

    async def expire_watchlist(self):
        """Deactivate expired watchlist items."""
        await self.db.execute(
            "UPDATE watchlist SET active = 0 WHERE expires_at < ?",
            (datetime.now().isoformat(),)
        )
        await self.db.commit()

    async def get_recent_deals(self, hours: int = 8, min_score: int = 0) -> list:
        """Get recent deals for digest."""
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        cursor = await self.db.execute("""
            SELECT product_name, sale_price, msrp, percent_off, sku,
                   retailer, mapper_url, score, tier, channel_name
            FROM seen_deals
            WHERE created_at > ? AND score >= ?
            ORDER BY score DESC
        """, (since, min_score))
        rows = await cursor.fetchall()
        return [
            {
                "product_name": r[0], "sale_price": r[1], "msrp": r[2],
                "percent_off": r[3], "sku": r[4], "retailer": r[5],
                "mapper_url": r[6], "score": r[7], "tier": r[8],
                "channel_name": r[9],
            }
            for r in rows
        ]

    async def cleanup_old(self, days: int = 30):
        """Remove old records."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        await self.db.execute("DELETE FROM seen_deals WHERE created_at < ?", (cutoff,))
        await self.db.execute("DELETE FROM alert_cooldowns WHERE last_alerted < ?", (cutoff,))
        await self.db.commit()

    async def close(self):
        """Close database connection."""
        if self.db:
            await self.db.close()
