"""
Profit Mapper Client - Authenticates via Discord OAuth and checks local inventory.

Uses Playwright for the initial Discord OAuth login, then maintains session
cookies for subsequent API requests via aiohttp. Auto-reauthenticates when
session expires using saved Discord cookies.
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger("deal_scout.mapper")

SESSION_FILE = "mapper_session.json"
DISCORD_COOKIES_FILE = "discord_cookies.json"


@dataclass
class StoreInventory:
    store_name: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    quantity: int = 0
    in_store_price: Optional[float] = None
    msrp: Optional[float] = None
    percent_off: Optional[float] = None
    distance_miles: Optional[float] = None
    pickup_available: bool = False
    last_seen: str = ""
    aisle_bay: str = ""
    google_maps_url: str = ""


@dataclass
class MapperResult:
    product_name: str = ""
    product_image_url: str = ""
    msrp: Optional[float] = None
    upc: Optional[str] = None
    sku: Optional[str] = None
    retailer: str = ""
    stores: list = field(default_factory=list)
    keepa_data_available: bool = False
    amazon_price: Optional[float] = None
    total_stores_checked: int = 0
    error: Optional[str] = None

    @property
    def has_local_stock(self) -> bool:
        return len(self.stores) > 0 and any(s.quantity > 0 for s in self.stores)

    @property
    def closest_store(self) -> Optional[StoreInventory]:
        stocked = [s for s in self.stores if s.quantity > 0]
        if not stocked:
            return None
        return min(stocked, key=lambda s: s.distance_miles or 999)

    @property
    def total_nearby_stock(self) -> int:
        return sum(s.quantity for s in self.stores)

    @property
    def best_price(self) -> Optional[float]:
        prices = [s.in_store_price for s in self.stores if s.in_store_price]
        return min(prices) if prices else None


class ProfitMapperClient:

    def __init__(self, config: dict):
        self.base_url = config.get("base_url", "https://profit-mapper.com").rstrip("/")
        self.zip_code = config.get("zip_code", "32259")
        self.radius = config.get("radius_miles", 50)
        self.session: Optional[aiohttp.ClientSession] = None
        self._authenticated = False
        self._auth_headers: dict = {}

    async def initialize(self):
        self.session = aiohttp.ClientSession(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            }
        )

        if os.path.exists(SESSION_FILE):
            try:
                with open(SESSION_FILE, "r") as f:
                    saved = json.load(f)

                cookie_url = self.base_url
                raw_cookies = saved.get("raw_cookies", [])
                if raw_cookies:
                    for c in raw_cookies:
                        self.session.cookie_jar.update_cookies(
                            {c["name"]: c["value"]},
                            response_url=yarl_url(cookie_url),
                        )
                else:
                    for name, value in saved.get("cookies", {}).items():
                        self.session.cookie_jar.update_cookies(
                            {name: value},
                            response_url=yarl_url(cookie_url),
                        )

                self._auth_headers = saved.get("auth_headers", {})
                if self._auth_headers:
                    self.session.headers.update(self._auth_headers)

                self._authenticated = True
                logger.info("Loaded saved Profit Mapper session")
            except Exception as e:
                logger.warning(f"Failed to load saved session: {e}")

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self, headless: bool = False):
        """
        Authenticate with Profit Mapper via Discord OAuth.

        headless=False (default): Opens a visible browser for manual login.
                                  Saves Discord cookies for future auto-reauth.
        headless=True:            Uses saved Discord cookies to auto-complete
                                  the OAuth flow without user interaction.
        """
        if not headless:
            logger.info("Starting manual Profit Mapper authentication...")
            print("\n" + "=" * 60)
            print("PROFIT MAPPER LOGIN")
            print("=" * 60)
            print("A browser window will open. Sign in with your Discord account.")
            print("The window will close automatically when done.")
            print("=" * 60 + "\n")
        else:
            logger.info("Starting automatic Profit Mapper re-authentication...")

        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=headless)
                context = await browser.new_context()

                # For auto-reauth: load saved Discord cookies so OAuth
                # auto-approves without user interaction
                if headless and os.path.exists(DISCORD_COOKIES_FILE):
                    try:
                        with open(DISCORD_COOKIES_FILE, "r") as f:
                            discord_cookies = json.load(f)
                        await context.add_cookies(discord_cookies)
                        logger.info(f"Loaded {len(discord_cookies)} Discord cookies")
                    except Exception as e:
                        logger.warning(f"Failed to load Discord cookies: {e}")
                        await browser.close()
                        raise RuntimeError(
                            "Auto-reauth failed: no Discord cookies. "
                            "Run --login manually first."
                        )

                page = await context.new_page()
                await page.goto(f"{self.base_url}/inventory-checker")

                if not headless:
                    print("Waiting for you to sign in...")

                # Wait for OAuth to complete and land back on profit-mapper
                try:
                    await page.wait_for_url(
                        f"{self.base_url}/**",
                        timeout=60000 if headless else 120000,
                    )
                except Exception:
                    pass

                # Wait for Auth.js session cookie
                session_found = False
                for _ in range(20):
                    await asyncio.sleep(1)
                    cookies = await context.cookies()
                    if any("session" in c["name"].lower() for c in cookies):
                        session_found = True
                        if not headless:
                            print("Session cookie detected!")
                        break

                if not session_found:
                    await asyncio.sleep(5)

                # Capture Profit Mapper cookies
                all_cookies = await context.cookies()
                raw_cookies = []
                has_session = False
                for cookie in all_cookies:
                    domain = cookie.get("domain", "")
                    if "profit-mapper" in domain:
                        raw_cookies.append({
                            "name": cookie["name"],
                            "value": cookie["value"],
                            "domain": domain,
                            "path": cookie.get("path", "/"),
                            "secure": cookie.get("secure", False),
                            "httpOnly": cookie.get("httpOnly", False),
                        })
                        if "session" in cookie["name"].lower():
                            has_session = True

                if not has_session:
                    msg = "No session cookie captured — auth may have failed"
                    logger.warning(msg)
                    if not headless:
                        print(f"WARNING: {msg}")
                        print(f"Cookies: {[c['name'] for c in raw_cookies]}")

                # During manual login: also save Discord cookies for auto-reauth
                if not headless:
                    discord_cookies = []
                    for cookie in all_cookies:
                        domain = cookie.get("domain", "")
                        if "discord" in domain:
                            discord_cookies.append({
                                "name": cookie["name"],
                                "value": cookie["value"],
                                "domain": domain,
                                "path": cookie.get("path", "/"),
                                "secure": cookie.get("secure", False),
                                "httpOnly": cookie.get("httpOnly", False),
                            })
                    if discord_cookies:
                        with open(DISCORD_COOKIES_FILE, "w") as f:
                            json.dump(discord_cookies, f, indent=2)
                        logger.info(f"Saved {len(discord_cookies)} Discord cookies for auto-reauth")

                # Update aiohttp session
                cookie_url = self.base_url
                for c in raw_cookies:
                    self.session.cookie_jar.update_cookies(
                        {c["name"]: c["value"]},
                        response_url=yarl_url(cookie_url),
                    )

                # Save session
                session_data = {
                    "raw_cookies": raw_cookies,
                    "auth_headers": {},
                }
                with open(SESSION_FILE, "w") as f:
                    json.dump(session_data, f, indent=2)

                self._authenticated = True
                logger.info("Authentication successful!")
                if not headless:
                    print(f"\nCookies captured: {len(raw_cookies)} (session: {'yes' if has_session else 'NO'})")
                    print("[OK] Logged in successfully! Session saved.\n")

                await browser.close()

        except ImportError:
            logger.error("Playwright not installed. Run: playwright install chromium")
            if not headless:
                print("\n[ERROR] Playwright not installed.")
                print("Run: pip install playwright && playwright install chromium\n")
            raise
        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            if not headless:
                print(f"\n[ERROR] Authentication failed: {e}\n")
            raise

    async def auto_reauthenticate(self) -> bool:
        """
        Try to re-authenticate automatically using saved Discord cookies.
        Returns True on success, False if manual login is needed.
        """
        if not os.path.exists(DISCORD_COOKIES_FILE):
            logger.info("No Discord cookies saved — manual login required")
            return False

        try:
            await self.authenticate(headless=True)
            # Verify it actually worked
            valid = await self.check_session()
            if valid:
                logger.info("Auto-reauthentication successful!")
                return True
            else:
                logger.warning("Auto-reauth completed but session check failed")
                return False
        except Exception as e:
            logger.warning(f"Auto-reauthentication failed: {e}")
            return False

    # ------------------------------------------------------------------
    # Session check
    # ------------------------------------------------------------------

    async def check_session(self) -> bool:
        if not self._authenticated or not self.session:
            return False

        try:
            # Hit the API directly — faster and more reliable than checking HTML
            async with self.session.get(
                f"{self.base_url}/api/users/@me/access",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 401 or resp.status == 403:
                    self._authenticated = False
                    logger.info("Session expired: API returned 401/403")
                    return False
                if resp.status == 200:
                    return True
                # Any other status — try the page-based check as fallback
                logger.debug(f"API session check got HTTP {resp.status}, trying page check")
        except Exception as e:
            logger.debug(f"API session check failed: {e}")

        # Fallback: check the HTML page
        try:
            async with self.session.get(
                f"{self.base_url}/inventory-checker",
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                final_url = str(resp.url)
                if "discord.com" in final_url or "/login" in final_url:
                    self._authenticated = False
                    return False
                return resp.status == 200
        except Exception as e:
            logger.error(f"Session check failed: {e}")
            return False

    # ------------------------------------------------------------------
    # Inventory checks via direct API
    # ------------------------------------------------------------------

    async def check_inventory(self, mapper_url: str) -> MapperResult:
        """
        Check inventory via Profit Mapper's internal API.
        Parses retailer and SKU from the mapper URL, calls
        /api/crawler/stores/crawl directly. Fast, no browser needed.
        """
        result = MapperResult()

        if not self._authenticated:
            result.error = "Not authenticated"
            return result

        url_match = re.search(
            r'/inventory-checker/(\w+)\?(?:sku|upc|productId)=(\w+)',
            mapper_url,
        )
        if not url_match:
            result.error = f"Could not parse retailer/SKU from URL: {mapper_url}"
            return result

        source_code = url_match.group(1)
        product_id = url_match.group(2)

        api_url = (
            f"{self.base_url}/api/crawler/stores/crawl"
            f"?sourceCode={source_code}&productId={product_id}"
            f"&radius={self.radius}&zipCode={self.zip_code}&v=1"
        )

        try:
            logger.debug(f"API check: {api_url}")
            async with self.session.get(
                api_url,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status in (401, 403):
                    self._authenticated = False
                    result.error = "Session expired"
                    return result
                if resp.status != 200:
                    result.error = f"API HTTP {resp.status}"
                    return result

                data = await resp.json()
                if data is None:
                    result.error = "Product not found on Profit Mapper"
                    return result
                return self._parse_crawl_api(data, mapper_url)

        except asyncio.TimeoutError:
            result.error = "Request timed out"
            return result
        except Exception as e:
            result.error = str(e)
            logger.error(f"Inventory API check failed: {e}")
            return result

    def _parse_crawl_api(self, data: dict, original_url: str) -> MapperResult:
        """Parse the /api/crawler/stores/crawl response."""
        result = MapperResult()
        result.product_name = data.get("title", "")
        result.msrp = data.get("msrp")
        result.upc = data.get("upc")
        result.sku = data.get("id") or data.get("itemNumber")
        result.product_image_url = (data.get("images") or [""])[0]

        amazon = data.get("amazon", {})
        if amazon and amazon.get("found"):
            result.keepa_data_available = True
            parsed = amazon.get("parsed", {})
            result.amazon_price = parsed.get("buyBox")

        for s in data.get("stores", []):
            store = StoreInventory(
                store_name=s.get("storeName", s.get("name", "")),
                address=s.get("address", ""),
                city=s.get("city", ""),
                state=s.get("state", ""),
                zip_code=str(s.get("zipCode", s.get("zip", ""))),
                quantity=_int(s.get("quantity", s.get("qty", s.get("onHand", 0)))),
                in_store_price=s.get("price", s.get("inStorePrice")),
                msrp=s.get("msrp"),
                percent_off=s.get("percentOff", s.get("discount")),
                distance_miles=_distance_to_miles(s.get("distance", s.get("distanceMiles"))),
                pickup_available=s.get("pickupAvailable", s.get("bopusEligible", False)),
                aisle_bay=s.get("aisle", s.get("bay", s.get("location", ""))),
            )
            result.stores.append(store)

        result.total_stores_checked = len(result.stores)
        self._detect_retailer(result, original_url)
        return result

    async def check_inventory_api(self, retailer: str, sku: str) -> MapperResult:
        retailer_slug = {
            "Home Depot": "hd", "Walmart": "walmart", "Target": "target",
            "Lowe's": "lowes", "Best Buy": "bestbuy", "Dollar General": "dg",
            "Sam's Club": "sams", "Costco": "costco", "Tractor Supply": "tsc",
            "GameStop": "gamestop", "BJ's": "bjs",
        }.get(retailer, retailer.lower())

        url = f"{self.base_url}/inventory-checker/{retailer_slug}?sku={sku}"
        return await self.check_inventory(url)

    def _detect_retailer(self, result: MapperResult, url: str):
        url_lower = url.lower()
        for pattern, name in {
            "target": "Target", "walmart": "Walmart",
            "homedepot": "Home Depot", "/hd": "Home Depot",
            "lowes": "Lowe's", "bestbuy": "Best Buy",
            "/dg": "Dollar General", "sams": "Sam's Club",
            "costco": "Costco", "/tsc": "Tractor Supply",
            "gamestop": "GameStop", "/bjs": "BJ's",
        }.items():
            if pattern in url_lower:
                result.retailer = name
                return

    async def close(self):
        if self.session:
            await self.session.close()


def yarl_url(url_str: str):
    from yarl import URL
    return URL(url_str)


def _int(value) -> int:
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def _distance_to_miles(value) -> Optional[float]:
    """Convert distance from API (meters) to miles."""
    if value is None:
        return None
    try:
        meters = float(value)
        # API returns meters — convert to miles
        # (values > 500 are almost certainly meters, not miles)
        if meters > 500:
            return round(meters / 1609.34, 1)
        return round(meters, 1)  # Already in miles
    except (ValueError, TypeError):
        return None
