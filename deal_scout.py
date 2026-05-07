"""
Deal Scout - Your personal Profit Lounge deal radar.

Monitors all deal channels, checks local inventory via Profit Mapper,
and DMs you only the deals worth grabbing near St. Johns, FL.

Usage:
    python deal_scout.py              # Run the bot
    python deal_scout.py --login      # Re-authenticate with Profit Mapper
    python deal_scout.py --test       # Test with a sample message
"""

import asyncio
import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

# Fix Unicode output on Windows consoles (PowerShell, cmd)
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

import yaml
import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from deal_parser import parse_message, check_hot_keywords, ParsedDeal
from mapper_client import ProfitMapperClient
from deal_scorer import DealScorer
from database import DealDatabase
from alert_formatter import format_deal_alert, format_digest, format_watchlist_update

# Load config
with open("config.yaml", "r") as f:
    CONFIG = yaml.safe_load(f)


def setup_logging():
    """Configure logging."""
    log_config = CONFIG.get("logging", {})
    level = getattr(logging, log_config.get("level", "INFO"))

    # Console handler with UTF-8 safe encoding
    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(level)
    console_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S"
    )
    console.setFormatter(console_fmt)

    # File handler
    file_handler = RotatingFileHandler(
        log_config.get("file", "deal_scout.log"),
        maxBytes=log_config.get("max_size_mb", 10) * 1024 * 1024,
        backupCount=log_config.get("backup_count", 3),
    )
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    file_handler.setFormatter(file_fmt)

    root = logging.getLogger("deal_scout")
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(file_handler)

    return root


logger = setup_logging()


class DealScoutBot(discord.Client):
    """Main bot that monitors Profit Lounge and alerts on good deals."""

    def __init__(self, config: dict):
        # discord.py-self uses Client, not Bot
        super().__init__()
        self.config = config
        self.discord_config = config["discord"]
        self.server_id = int(self.discord_config["server_id"])
        self.my_user_id = int(self.discord_config["my_user_id"])

        # Channel detection
        self.watch_channel_ids = set()
        self.channel_patterns = self.discord_config.get("deal_channel_patterns", [])

        # Components
        self.mapper = ProfitMapperClient(config.get("profit_mapper", {}))
        self.scorer = DealScorer(config)
        self.db = DealDatabase()

        # Scheduler for digests and watchlist checks
        self.scheduler = AsyncIOScheduler()

        # Alert queue for quiet hours
        self.alert_queue = []

        # Rate limiting
        self._mapper_semaphore = asyncio.Semaphore(3)  # Max concurrent mapper checks
        self._last_alert_time = datetime.min

    async def on_ready(self):
        """Bot is connected and ready."""
        logger.info(f"Logged in as {self.user}")

        # Initialize components
        await self.db.initialize()
        await self.mapper.initialize()

        # Check mapper session
        if not await self.mapper.check_session():
            logger.warning("Profit Mapper session expired! Run with --login to re-authenticate")
            # DM myself about it
            try:
                me = await self.fetch_user(self.my_user_id)
                await me.send(
                    "⚠️ **Deal Scout**: Profit Mapper session expired. "
                    "Run `python deal_scout.py --login` to re-authenticate."
                )
            except Exception:
                pass

        # Detect deal channels in Profit Lounge
        await self._detect_channels()

        # Set up scheduled tasks
        self._setup_schedules()

        logger.info(f"Monitoring {len(self.watch_channel_ids)} channels")
        logger.info("Deal Scout is running! Watching for deals...")

    async def _detect_channels(self):
        """Auto-detect deal channels in the Profit Lounge server."""
        # If specific channels are configured, use those
        configured = self.discord_config.get("watch_channels", [])
        if configured:
            self.watch_channel_ids = set(int(c) for c in configured)
            logger.info(f"Using {len(self.watch_channel_ids)} configured channels")
            return

        # Otherwise, auto-detect by name patterns
        guild = self.get_guild(self.server_id)
        if not guild:
            logger.error(f"Could not find server {self.server_id}")
            return

        for channel in guild.text_channels:
            name = channel.name.lower()
            for pattern in self.channel_patterns:
                if pattern.lower() in name:
                    self.watch_channel_ids.add(channel.id)
                    logger.debug(f"  Watching: #{channel.name} (matched '{pattern}')")
                    break

        logger.info(f"Auto-detected {len(self.watch_channel_ids)} deal channels")

    def _setup_schedules(self):
        """Set up scheduled tasks (digests, watchlist checks, cleanup)."""
        # Deal Digests
        digest_config = self.config.get("digest", {})
        if digest_config.get("enabled"):
            tz = digest_config.get("timezone", "America/New_York")
            for time_str in digest_config.get("times", []):
                hour, minute = time_str.split(":")
                self.scheduler.add_job(
                    self._send_digest,
                    CronTrigger(hour=int(hour), minute=int(minute), timezone=tz),
                    id=f"digest_{time_str}",
                )
                logger.info(f"Digest scheduled at {time_str} {tz}")

        # Watchlist re-checks
        tracker_config = self.config.get("tracker", {})
        if tracker_config.get("enabled"):
            interval = tracker_config.get("recheck_interval_minutes", 30)
            self.scheduler.add_job(
                self._check_watchlist,
                "interval",
                minutes=interval,
                id="watchlist_check",
            )
            logger.info(f"Watchlist checks every {interval} minutes")

        # Session health check
        mapper_config = self.config.get("profit_mapper", {})
        check_interval = mapper_config.get("session_check_interval_minutes", 30)
        self.scheduler.add_job(
            self._check_mapper_session,
            "interval",
            minutes=check_interval,
            id="session_check",
        )

        # Daily cleanup
        self.scheduler.add_job(
            self._daily_cleanup,
            CronTrigger(hour=3, minute=0, timezone="America/New_York"),
            id="daily_cleanup",
        )

        # Quiet hours queue flush
        alerts_config = self.config.get("alerts", {})
        quiet = alerts_config.get("quiet_hours", {})
        if quiet.get("enabled") and quiet.get("queue_during_quiet"):
            end_time = quiet.get("end", "07:00")
            hour, minute = end_time.split(":")
            tz = quiet.get("timezone", "America/New_York")
            self.scheduler.add_job(
                self._flush_alert_queue,
                CronTrigger(hour=int(hour), minute=int(minute), timezone=tz),
                id="flush_queue",
            )

        self.scheduler.start()

    async def on_message(self, message: discord.Message):
        """Process every message in watched channels."""
        # Only process messages from watched channels
        if message.channel.id not in self.watch_channel_ids:
            return

        # Don't process our own messages
        if message.author.id == self.user.id:
            return

        # Parse the message for deal info
        deal = parse_message(message)
        if not deal:
            return

        logger.info(f"Deal detected in #{deal.channel_name}: {deal.product_name[:50]}")

        # Check if we should ignore this deal
        if self.scorer.should_ignore(deal):
            logger.debug(f"Ignoring deal (matched ignore keyword)")
            return

        # Check for duplicates / cooldown
        sku_key = deal.sku or deal.product_name[:50]
        cooldown = self.config.get("alerts", {}).get("cooldown_minutes", 120)
        if await self.db.is_duplicate(deal.message_id, sku_key, cooldown):
            logger.debug(f"Duplicate/cooldown: {sku_key}")
            return

        # Check hot keywords
        hot_keywords = self.config.get("filters", {}).get("hot_keywords", [])
        check_hot_keywords(deal, hot_keywords)

        # Check Profit Mapper for local inventory
        mapper_result = None
        if deal.profit_mapper_url:
            async with self._mapper_semaphore:
                try:
                    mapper_result = await self.mapper.check_inventory(deal.profit_mapper_url)
                    if mapper_result.error:
                        logger.warning(f"Mapper error: {mapper_result.error}")
                except Exception as e:
                    logger.error(f"Mapper check failed: {e}")

        # Score the deal
        score_result = self.scorer.score(deal, mapper_result)

        # Record in database
        await self.db.record_deal({
            "message_id": deal.message_id,
            "channel_id": deal.channel_id,
            "channel_name": deal.channel_name,
            "product_name": deal.product_name,
            "sale_price": score_result.get("sale_price"),
            "msrp": deal.msrp,
            "percent_off": score_result.get("percent_off"),
            "sku": deal.sku,
            "retailer": deal.retailer,
            "mapper_url": deal.profit_mapper_url,
            "score": score_result["total_score"],
            "tier": score_result["tier"],
        })

        # Alert if it meets threshold
        if score_result["tier"] in ("hot", "solid"):
            alert_msg = format_deal_alert(deal, score_result, mapper_result)
            await self._send_alert(alert_msg, sku_key)

            logger.info(
                f"{'🔥 HOT' if score_result['tier'] == 'hot' else '📊 SOLID'} "
                f"[{score_result['total_score']}] {deal.product_name[:40]} "
                f"- ${score_result.get('sale_price', '?')}"
            )
        else:
            logger.debug(
                f"Skip [{score_result['total_score']}] {deal.product_name[:40]}"
            )

    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User):
        """Handle pin reactions to add deals to watchlist."""
        if user.id != self.my_user_id:
            return

        tracker_config = self.config.get("tracker", {})
        pin_emoji = tracker_config.get("pin_emoji", "📌")

        if str(reaction.emoji) != pin_emoji:
            return

        # Check if this is one of our alert DMs
        if not isinstance(reaction.message.channel, discord.DMChannel):
            return

        # Try to extract mapper URL from the message
        content = reaction.message.content
        mapper_url = None
        import re
        match = re.search(r'https?://profit-mapper\.com[^\s\)]+', content)
        if match:
            mapper_url = match.group(0)

        # Extract product name (first bold text)
        name_match = re.search(r'\*\*(.+?)\*\*', content)
        product_name = name_match.group(1) if name_match else "Tracked Deal"

        expire_days = tracker_config.get("auto_expire_days", 7)
        await self.db.add_to_watchlist({
            "message_id": str(reaction.message.id),
            "product_name": product_name,
            "mapper_url": mapper_url,
            "sku": None,
            "retailer": None,
            "sale_price": None,
        }, expire_days=expire_days)

        # Confirm to user
        try:
            await reaction.message.add_reaction("✅")
        except Exception:
            pass

        logger.info(f"Added to watchlist: {product_name}")

    async def _send_alert(self, message: str, sku_key: str):
        """Send a DM alert, respecting quiet hours."""
        if self._is_quiet_hours():
            queue_enabled = self.config.get("alerts", {}).get("quiet_hours", {}).get("queue_during_quiet", True)
            if queue_enabled:
                self.alert_queue.append(message)
                logger.debug("Alert queued (quiet hours)")
                return
            else:
                return

        try:
            me = await self.fetch_user(self.my_user_id)
            # Split long messages
            if len(message) > 2000:
                message = message[:1997] + "..."
            await me.send(message)
            await self.db.record_alert(sku_key)
            logger.debug("Alert sent")
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")

    async def _flush_alert_queue(self):
        """Send all queued alerts from quiet hours."""
        if not self.alert_queue:
            return

        logger.info(f"Flushing {len(self.alert_queue)} queued alerts")
        try:
            me = await self.fetch_user(self.my_user_id)
            await me.send(f"🌅 **Good morning!** {len(self.alert_queue)} deals came in overnight:\n")

            for alert in self.alert_queue[:20]:  # Cap at 20
                if len(alert) > 2000:
                    alert = alert[:1997] + "..."
                await me.send(alert)
                await asyncio.sleep(1)  # Rate limit

            if len(self.alert_queue) > 20:
                await me.send(f"... and {len(self.alert_queue) - 20} more. Check the digest!")

        except Exception as e:
            logger.error(f"Failed to flush queue: {e}")
        finally:
            self.alert_queue.clear()

    async def _send_digest(self):
        """Send a scheduled deal digest."""
        lookback = self.config.get("digest", {}).get("lookback_hours", 8)
        min_score = self.config.get("filters", {}).get("solid_threshold", 40)
        deals = await self.db.get_recent_deals(hours=lookback, min_score=min_score)

        message = format_digest(deals, f"in the last {lookback} hours")

        try:
            me = await self.fetch_user(self.my_user_id)
            await me.send(message)
            logger.info(f"Digest sent: {len(deals)} deals")
        except Exception as e:
            logger.error(f"Failed to send digest: {e}")

    async def _check_watchlist(self):
        """Re-check inventory for watchlisted deals."""
        items = await self.db.get_active_watchlist()
        if not items:
            return

        logger.debug(f"Checking {len(items)} watchlist items")

        for item in items:
            if not item.get("mapper_url"):
                continue

            async with self._mapper_semaphore:
                try:
                    result = await self.mapper.check_inventory(item["mapper_url"])

                    if result.error:
                        continue

                    new_stock = result.total_nearby_stock
                    new_price = result.best_price
                    old_stock = item.get("last_stock", 0)
                    old_price = item.get("last_price")

                    # Detect changes
                    if old_stock == 0 and new_stock > 0:
                        msg = format_watchlist_update(
                            item["product_name"], "restock",
                            new_value=new_stock, mapper_url=item["mapper_url"]
                        )
                        await self._send_alert(msg, item.get("sku", "watchlist"))

                    elif new_price and old_price and new_price < old_price * 0.9:
                        msg = format_watchlist_update(
                            item["product_name"], "price_drop",
                            old_value=old_price, new_value=new_price,
                            mapper_url=item["mapper_url"]
                        )
                        await self._send_alert(msg, item.get("sku", "watchlist"))

                    elif old_stock > 3 and new_stock <= 2 and new_stock > 0:
                        msg = format_watchlist_update(
                            item["product_name"], "low_stock",
                            old_value=old_stock, new_value=new_stock,
                            mapper_url=item["mapper_url"]
                        )
                        await self._send_alert(msg, item.get("sku", "watchlist"))

                    # Update database
                    await self.db.update_watchlist_item(
                        item["id"], price=new_price, stock=new_stock
                    )

                except Exception as e:
                    logger.error(f"Watchlist check failed for {item['product_name']}: {e}")

                await asyncio.sleep(2)  # Be gentle with mapper

    async def _check_mapper_session(self):
        """Periodically verify Profit Mapper session is alive."""
        if not await self.mapper.check_session():
            logger.warning("Profit Mapper session expired!")
            try:
                me = await self.fetch_user(self.my_user_id)
                await me.send(
                    "⚠️ **Deal Scout**: Profit Mapper session expired. "
                    "Inventory checks are paused. Run:\n"
                    "```python deal_scout.py --login```"
                )
            except Exception:
                pass

    async def _daily_cleanup(self):
        """Daily maintenance tasks."""
        await self.db.cleanup_old(days=30)
        await self.db.expire_watchlist()
        logger.info("Daily cleanup complete")

    def _is_quiet_hours(self) -> bool:
        """Check if we're in quiet hours."""
        quiet = self.config.get("alerts", {}).get("quiet_hours", {})
        if not quiet.get("enabled"):
            return False

        tz_name = quiet.get("timezone", "America/New_York")
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo(tz_name))
        except Exception:
            now = datetime.now()

        start_h, start_m = map(int, quiet.get("start", "23:00").split(":"))
        end_h, end_m = map(int, quiet.get("end", "07:00").split(":"))

        start = now.replace(hour=start_h, minute=start_m, second=0)
        end = now.replace(hour=end_h, minute=end_m, second=0)

        if start > end:
            return now >= start or now <= end
        else:
            return start <= now <= end


async def do_login(config):
    """Run the Profit Mapper login flow."""
    mapper = ProfitMapperClient(config.get("profit_mapper", {}))
    await mapper.initialize()
    await mapper.authenticate()
    await mapper.close()


def main():
    parser = argparse.ArgumentParser(description="Deal Scout - Profit Lounge Deal Radar")
    parser.add_argument("--login", action="store_true", help="Authenticate with Profit Mapper")
    parser.add_argument("--test", action="store_true", help="Run a test check")
    args = parser.parse_args()

    if args.login:
        asyncio.run(do_login(CONFIG))
        print("\n✅ You can now run the bot: python deal_scout.py")
        return

    # Validate config
    token = CONFIG["discord"]["token"]
    if token == "YOUR_DISCORD_TOKEN_HERE":
        print("❌ Please configure your Discord token in config.yaml")
        print("   See the config file for instructions on getting your token.")
        sys.exit(1)

    if CONFIG["discord"]["my_user_id"] == "YOUR_USER_ID_HERE":
        print("❌ Please configure your Discord user ID in config.yaml")
        sys.exit(1)

    if CONFIG["discord"]["server_id"] == "YOUR_SERVER_ID_HERE":
        print("❌ Please configure the Profit Lounge server ID in config.yaml")
        sys.exit(1)

    print("""
    ╔══════════════════════════════════════╗
    ║          🔍 DEAL SCOUT 🔍           ║
    ║   Profit Lounge Deal Radar           ║
    ║   St. Johns, FL · 32259              ║
    ╚══════════════════════════════════════╝
    """)

    bot = DealScoutBot(CONFIG)
    bot.run(token)


if __name__ == "__main__":
    main()
