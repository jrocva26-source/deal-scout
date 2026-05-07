"""
Profit Mapper Client - Authenticates via Discord OAuth and checks local inventory.

Uses Playwright for the initial Discord OAuth login, then maintains session
cookies for subsequent requests. Falls back to Playwright-based inventory
checks if the site uses client-side rendering that aiohttp can't handle.
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
        self._api_endpoints: list[dict] = []
        self._auth_headers: dict = {}
        self._use_playwright_for_checks = False

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

                # Restore full cookie objects with domain info
                cookie_url = self.base_url
                raw_cookies = saved.get("raw_cookies", [])
                if raw_cookies:
                    for c in raw_cookies:
                        self.session.cookie_jar.update_cookies(
                            {c["name"]: c["value"]},
                            response_url=yarl_url(cookie_url),
                        )
                else:
                    # Legacy format: flat name:value dict
                    for name, value in saved.get("cookies", {}).items():
                        self.session.cookie_jar.update_cookies(
                            {name: value},
                            response_url=yarl_url(cookie_url),
                        )

                # Restore auth headers (e.g. Bearer token)
                self._auth_headers = saved.get("auth_headers", {})
                if self._auth_headers:
                    self.session.headers.update(self._auth_headers)

                # Restore discovered API endpoints
                self._api_endpoints = saved.get("api_endpoints", [])

                self._use_playwright_for_checks = saved.get("use_playwright", False)
                self._authenticated = True
                logger.info("Loaded saved Profit Mapper session")
            except Exception as e:
                logger.warning(f"Failed to load saved session: {e}")

    async def authenticate(self):
        logger.info("Starting Profit Mapper authentication...")
        print("\n" + "=" * 60)
        print("PROFIT MAPPER LOGIN")
        print("=" * 60)
        print("A browser window will open. Sign in with your Discord account.")
        print("The window will close automatically when done.")
        print("=" * 60 + "\n")

        try:
            from playwright.async_api import async_playwright

            captured_api_calls = []
            captured_headers = {}

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=False)
                context = await browser.new_context()
                page = await context.new_page()

                # Intercept network requests to discover API endpoints and auth headers
                async def on_request(request):
                    url = request.url
                    if "profit-mapper.com" in url and "/api/" in url:
                        captured_api_calls.append({
                            "url": url,
                            "method": request.method,
                        })
                        auth = request.headers.get("authorization", "")
                        if auth:
                            captured_headers["Authorization"] = auth
                        # Check for other common auth headers
                        for h in ("x-auth-token", "x-csrf-token", "x-session-token"):
                            val = request.headers.get(h, "")
                            if val:
                                captured_headers[h] = val

                page.on("request", on_request)

                await page.goto(f"{self.base_url}/inventory-checker")

                print("Waiting for you to sign in...")
                # Wait for the OAuth callback to land back on profit-mapper
                try:
                    await page.wait_for_url(
                        f"{self.base_url}/**",
                        timeout=120000,
                    )
                except Exception:
                    pass

                # Auth.js (NextAuth) sets the session cookie AFTER the OAuth
                # callback completes. Wait for it specifically.
                print("Waiting for session cookie...")
                for _ in range(20):  # up to ~20 seconds
                    await asyncio.sleep(1)
                    cookies = await context.cookies()
                    cookie_names = [c["name"] for c in cookies]
                    if any("session" in n.lower() for n in cookie_names):
                        print("Session cookie detected!")
                        break
                else:
                    # Give it a final generous wait
                    await asyncio.sleep(5)

                # Navigate to an inventory page to trigger API calls so we can
                # capture the real auth mechanism
                current_url = page.url
                if "/inventory-checker" not in current_url:
                    try:
                        await page.goto(f"{self.base_url}/inventory-checker")
                        await asyncio.sleep(3)
                    except Exception:
                        pass

                # Grab ALL cookies from profit-mapper domain
                all_cookies = await context.cookies()
                raw_cookies = []
                session_cookie_found = False
                for cookie in all_cookies:
                    domain = cookie.get("domain", "")
                    if "profit-mapper" in domain or "profit_mapper" in domain:
                        raw_cookies.append({
                            "name": cookie["name"],
                            "value": cookie["value"],
                            "domain": domain,
                            "path": cookie.get("path", "/"),
                            "secure": cookie.get("secure", False),
                            "httpOnly": cookie.get("httpOnly", False),
                        })
                        if "session" in cookie["name"].lower():
                            session_cookie_found = True
                            logger.info(f"Session cookie: {cookie['name']}")

                if not session_cookie_found:
                    print("WARNING: No session cookie found! Auth may not have completed.")
                    print(f"Cookies captured: {[c['name'] for c in raw_cookies]}")
                    print("Try running --login again and wait for the page to fully load.")

                # Also grab localStorage tokens
                local_storage = {}
                try:
                    local_storage = await page.evaluate("""
                        () => {
                            const items = {};
                            for (let i = 0; i < localStorage.length; i++) {
                                const key = localStorage.key(i);
                                items[key] = localStorage.getItem(key);
                            }
                            return items;
                        }
                    """)
                except Exception:
                    pass

                # Look for auth tokens in localStorage
                for key, value in local_storage.items():
                    key_lower = key.lower()
                    if any(t in key_lower for t in ("token", "auth", "session", "jwt", "access")):
                        logger.info(f"Found localStorage auth key: {key}")
                        # Try to determine if it's a Bearer token
                        if value and not value.startswith("{"):
                            captured_headers["Authorization"] = f"Bearer {value}"
                        elif value and value.startswith("{"):
                            try:
                                token_data = json.loads(value)
                                for tk in ("accessToken", "access_token", "token", "id_token"):
                                    if tk in token_data:
                                        captured_headers["Authorization"] = f"Bearer {token_data[tk]}"
                                        break
                            except json.JSONDecodeError:
                                pass

                # Also check sessionStorage
                try:
                    session_storage = await page.evaluate("""
                        () => {
                            const items = {};
                            for (let i = 0; i < sessionStorage.length; i++) {
                                const key = sessionStorage.key(i);
                                items[key] = sessionStorage.getItem(key);
                            }
                            return items;
                        }
                    """)
                    for key, value in session_storage.items():
                        key_lower = key.lower()
                        if any(t in key_lower for t in ("token", "auth", "session")):
                            logger.info(f"Found sessionStorage auth key: {key}")
                            if value and not value.startswith("{"):
                                captured_headers.setdefault("Authorization", f"Bearer {value}")
                except Exception:
                    pass

                # Check if the page content loaded (SPA detection)
                page_html = await page.content()
                has_server_content = bool(BeautifulSoup(page_html, "lxml").select_one("table"))
                if not has_server_content:
                    logger.info("Page appears to be SPA-rendered (no server HTML tables)")
                    self._use_playwright_for_checks = not captured_api_calls

                # Set cookies on aiohttp session with proper URL context
                cookie_url = self.base_url
                for c in raw_cookies:
                    self.session.cookie_jar.update_cookies(
                        {c["name"]: c["value"]},
                        response_url=yarl_url(cookie_url),
                    )

                # Set auth headers
                self._auth_headers = captured_headers
                if captured_headers:
                    self.session.headers.update(captured_headers)
                    logger.info(f"Captured auth headers: {list(captured_headers.keys())}")

                self._api_endpoints = captured_api_calls
                if captured_api_calls:
                    logger.info(f"Discovered {len(captured_api_calls)} API endpoints")
                    for ep in captured_api_calls[:5]:
                        logger.debug(f"  {ep['method']} {ep['url']}")

                # Save session
                session_data = {
                    "raw_cookies": raw_cookies,
                    "auth_headers": captured_headers,
                    "api_endpoints": captured_api_calls,
                    "local_storage_keys": list(local_storage.keys()),
                    "use_playwright": self._use_playwright_for_checks,
                }
                with open(SESSION_FILE, "w") as f:
                    json.dump(session_data, f, indent=2)

                self._authenticated = True
                logger.info("Authentication successful!")
                print(f"\nCookies captured: {len(raw_cookies)}")
                print(f"Auth headers: {list(captured_headers.keys()) or 'none (cookie-based)'}")
                print(f"API endpoints found: {len(captured_api_calls)}")
                print(f"SPA mode: {'yes' if self._use_playwright_for_checks else 'no'}")
                print("\n[OK] Logged in successfully! Session saved.\n")

                await browser.close()

        except ImportError:
            logger.error("Playwright not installed. Run: playwright install chromium")
            print("\n[ERROR] Playwright not installed.")
            print("Run: pip install playwright && playwright install chromium\n")
            raise
        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            print(f"\n[ERROR] Authentication failed: {e}\n")
            raise

    async def check_session(self) -> bool:
        if not self._authenticated or not self.session:
            return False

        try:
            # First try with redirects allowed so we can check the final URL
            # and page content, not just the initial redirect
            async with self.session.get(
                f"{self.base_url}/inventory-checker",
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                final_url = str(resp.url)

                # Redirected to Discord OAuth or login page = expired
                if "discord.com" in final_url or "/login" in final_url:
                    self._authenticated = False
                    logger.info("Session expired: redirected to login")
                    return False

                if resp.status != 200:
                    logger.info(f"Session check got HTTP {resp.status}")
                    return resp.status < 400

                # For SPAs: check if the HTML contains auth-gated content or
                # a sign that we're authenticated (not just the app shell)
                html = await resp.text()

                # If the page contains a "sign in" or "login" button/prompt
                # in the main content, we're probably not authenticated
                html_lower = html.lower()
                unauthenticated_signals = [
                    "sign in with discord",
                    "login with discord",
                    "connect with discord",
                    "please log in",
                    "please sign in",
                    '"authenticated":false',
                    '"isAuthenticated":false',
                    '"loggedIn":false',
                ]
                for signal in unauthenticated_signals:
                    if signal in html_lower:
                        self._authenticated = False
                        logger.info(f"Session expired: page contains '{signal}'")
                        return False

                # Positive signals that we're authenticated
                authenticated_signals = [
                    '"authenticated":true',
                    '"isAuthenticated":true',
                    '"loggedIn":true',
                    "inventory-checker",
                    "sign out",
                    "log out",
                    "my account",
                ]
                for signal in authenticated_signals:
                    if signal in html_lower:
                        logger.debug(f"Session valid: found '{signal}'")
                        return True

                # If we got a 200 and no clear signal either way, assume valid
                logger.debug("Session check: 200 with no clear auth signals, assuming valid")
                return True

        except Exception as e:
            logger.error(f"Session check failed: {e}")
            return False

    async def check_inventory(self, mapper_url: str) -> MapperResult:
        """
        Check inventory via Profit Mapper's internal API.

        Parses the retailer and SKU from the mapper URL, then calls
        /api/crawler/stores/crawl directly. Fast — no browser needed.
        """
        result = MapperResult()

        if not self._authenticated:
            result.error = "Not authenticated"
            return result

        # Extract retailer slug and SKU from mapper URL
        # e.g. https://profit-mapper.com/inventory-checker/hd?sku=317991357
        import re
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
            f"&radius={self.radius}&v=1"
        )

        try:
            logger.debug(f"API check: {api_url}")
            async with self.session.get(
                api_url,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 401 or resp.status == 403:
                    self._authenticated = False
                    result.error = "Session expired"
                    return result
                if resp.status != 200:
                    result.error = f"API HTTP {resp.status}"
                    return result

                data = await resp.json()
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

        # Amazon/Keepa data
        amazon = data.get("amazon", {})
        if amazon and amazon.get("found"):
            result.keepa_data_available = True
            parsed = amazon.get("parsed", {})
            result.amazon_price = parsed.get("buyBox")

        # Stores
        stores_list = data.get("stores", [])
        for s in stores_list:
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
                distance_miles=s.get("distance", s.get("distanceMiles")),
                pickup_available=s.get("pickupAvailable", s.get("bopusEligible", False)),
                aisle_bay=s.get("aisle", s.get("bay", s.get("location", ""))),
            )
            result.stores.append(store)

        result.total_stores_checked = len(result.stores)
        self._detect_retailer(result, original_url)
        return result

    async def _check_inventory_playwright(self, mapper_url: str) -> MapperResult:
        """Use Playwright to load the inventory page (handles SPA rendering)."""
        result = MapperResult()
        url = self._build_url(mapper_url)

        try:
            from playwright.async_api import async_playwright

            captured_api_data = {}

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context()

                # Load saved cookies into Playwright context
                if os.path.exists(SESSION_FILE):
                    try:
                        with open(SESSION_FILE, "r") as f:
                            saved = json.load(f)
                        raw_cookies = saved.get("raw_cookies", [])
                        if raw_cookies:
                            pw_cookies = []
                            for c in raw_cookies:
                                # Use url-based cookie setting for all cookies
                                # to avoid domain format issues with __Host-/__Secure- prefixes
                                pw_cookie = {
                                    "name": c["name"],
                                    "value": c["value"],
                                    "url": self.base_url,
                                }
                                if c.get("secure"):
                                    pw_cookie["secure"] = True
                                if c.get("httpOnly"):
                                    pw_cookie["httpOnly"] = True
                                pw_cookies.append(pw_cookie)
                            await context.add_cookies(pw_cookies)
                    except Exception as e:
                        logger.warning(f"Failed to load cookies into Playwright: {e}")

                page = await context.new_page()

                # Intercept API responses that contain inventory data
                async def on_response(response):
                    try:
                        resp_url = response.url
                        if "/api/" in resp_url and response.status == 200:
                            content_type = response.headers.get("content-type", "")
                            if "json" in content_type:
                                data = await response.json()
                                captured_api_data[resp_url] = data
                    except Exception:
                        pass

                page.on("response", on_response)

                await page.goto(url, wait_until="networkidle", timeout=30000)
                await asyncio.sleep(2)

                # First check captured API responses for inventory data
                for api_url, data in captured_api_data.items():
                    if isinstance(data, dict) and any(
                        k in data for k in ("stores", "locations", "inventory", "results")
                    ):
                        result = self._parse_api_response(data, mapper_url)
                        if result.stores:
                            await browser.close()
                            return result

                # Fall back to parsing the rendered HTML
                html = await page.content()
                result = self._parse_inventory_page(html, mapper_url)

                # If still no stores, try reading from the rendered DOM directly
                if not result.stores:
                    try:
                        dom_data = await page.evaluate("""
                            () => {
                                const rows = document.querySelectorAll('tr, [class*="store"], [class*="location"], [class*="inventory"]');
                                return Array.from(rows).map(r => r.innerText).filter(t => t.trim().length > 0);
                            }
                        """)
                        if dom_data:
                            logger.debug(f"DOM returned {len(dom_data)} rows")
                            for row_text in dom_data:
                                store = self._parse_store_row_text(row_text)
                                if store and store.store_name:
                                    result.stores.append(store)
                    except Exception as e:
                        logger.debug(f"DOM evaluation failed: {e}")

                result.total_stores_checked = len(result.stores)
                await browser.close()
                return result

        except ImportError:
            result.error = "Playwright not installed"
            return result
        except Exception as e:
            result.error = f"Playwright check failed: {e}"
            logger.error(f"Playwright inventory check failed: {e}")
            return result

    def _parse_api_response(self, data: dict, original_url: str) -> MapperResult:
        """Parse inventory data from an intercepted API response."""
        result = MapperResult()

        result.product_name = data.get("productName", data.get("name", ""))
        result.msrp = data.get("msrp") or data.get("retailPrice")
        result.upc = data.get("upc")
        result.sku = data.get("sku") or data.get("productId")

        stores_key = next(
            (k for k in ("stores", "locations", "inventory", "results") if k in data),
            None,
        )
        if stores_key and isinstance(data[stores_key], list):
            for s in data[stores_key]:
                store = StoreInventory(
                    store_name=s.get("storeName", s.get("name", "")),
                    address=s.get("address", ""),
                    city=s.get("city", ""),
                    state=s.get("state", ""),
                    zip_code=s.get("zipCode", s.get("zip", "")),
                    quantity=s.get("quantity", s.get("qty", s.get("onHand", 0))),
                    in_store_price=s.get("price", s.get("inStorePrice")),
                    msrp=s.get("msrp", s.get("retailPrice")),
                    percent_off=s.get("percentOff", s.get("discount")),
                    distance_miles=s.get("distance", s.get("distanceMiles")),
                    pickup_available=s.get("pickupAvailable", s.get("bopusEligible", False)),
                    aisle_bay=s.get("aisle", s.get("bay", s.get("location", ""))),
                )
                if isinstance(store.quantity, str):
                    try:
                        store.quantity = int(store.quantity)
                    except ValueError:
                        store.quantity = 0
                result.stores.append(store)

        self._detect_retailer(result, original_url)
        result.total_stores_checked = len(result.stores)
        return result

    def _parse_store_row_text(self, text: str) -> Optional[StoreInventory]:
        """Try to parse a store from raw text of a table row or store card."""
        if not text or len(text) < 10:
            return None

        store = StoreInventory()
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if not lines:
            return None

        store.store_name = lines[0]

        for line in lines:
            # Quantity
            qty_match = re.search(r'(?:qty|quantity|stock)[:\s]*(\d+)', line, re.IGNORECASE)
            if qty_match:
                store.quantity = int(qty_match.group(1))
                continue

            # Price
            price_match = re.search(r'\$(\d+(?:\.\d{2})?)', line)
            if price_match and not store.in_store_price:
                store.in_store_price = float(price_match.group(1))

            # Distance
            dist_match = re.search(r'(\d+(?:\.\d+)?)\s*mi', line, re.IGNORECASE)
            if dist_match:
                store.distance_miles = float(dist_match.group(1))

            # Percent off
            pct_match = re.search(r'(\d+)%\s*off', line, re.IGNORECASE)
            if pct_match:
                store.percent_off = float(pct_match.group(1))

        return store if store.store_name else None

    def _build_url(self, mapper_url: str) -> str:
        return mapper_url

    def _parse_inventory_page(self, html: str, original_url: str) -> MapperResult:
        result = MapperResult()
        soup = BeautifulSoup(html, "lxml")

        title_el = soup.select_one("h1, h2, .product-title, .product-name")
        if title_el:
            result.product_name = title_el.get_text(strip=True)

        # MSRP
        msrp_el = soup.find(string=lambda t: t and "MSRP" in str(t))
        if msrp_el:
            msrp_match = re.search(r'\$(\d+(?:\.\d{2})?)', str(msrp_el))
            if msrp_match:
                result.msrp = float(msrp_match.group(1))

        # UPC
        upc_el = soup.find(string=lambda t: t and "UPC:" in str(t))
        if upc_el:
            upc_match = re.search(r'UPC:\s*(\d+)', str(upc_el))
            if upc_match:
                result.upc = upc_match.group(1)

        # SKU
        sku_el = soup.find(string=lambda t: t and "Product #:" in str(t))
        if sku_el:
            sku_match = re.search(r'Product\s*#:\s*(\d+)', str(sku_el))
            if sku_match:
                result.sku = sku_match.group(1)

        # Parse store table
        table = soup.select_one("table")
        if table:
            rows = table.select("tr")[1:]
            for row in rows:
                cells = row.select("td")
                if len(cells) < 4:
                    continue

                store = StoreInventory()

                store_cell = cells[0]
                if store_cell:
                    text_parts = store_cell.get_text(separator="\n").strip().split("\n")
                    if text_parts:
                        store.store_name = text_parts[0].strip()
                    if len(text_parts) > 1:
                        store.address = text_parts[1].strip()
                    if len(text_parts) > 2:
                        store.city = text_parts[2].strip()
                    for link in store_cell.select("a"):
                        href = link.get("href", "")
                        if "google.com/maps" in href or "maps.google" in href:
                            store.google_maps_url = href

                if len(cells) > 1:
                    pickup_text = cells[1].get_text(strip=True).lower()
                    store.pickup_available = "available" in pickup_text

                if len(cells) > 2:
                    qty_text = cells[2].get_text(strip=True)
                    try:
                        store.quantity = int(qty_text)
                    except ValueError:
                        store.quantity = 0

                if len(cells) > 3:
                    price_text = cells[3].get_text(strip=True)
                    price_match = re.search(r'\$?(\d+(?:\.\d{2})?)', price_text)
                    if price_match:
                        store.in_store_price = float(price_match.group(1))

                if len(cells) > 4:
                    pct_text = cells[4].get_text(strip=True)
                    pct_match = re.search(r'(\d+)%', pct_text)
                    if pct_match:
                        store.percent_off = float(pct_match.group(1))

                if len(cells) > 5:
                    dist_text = cells[5].get_text(strip=True)
                    try:
                        store.distance_miles = float(dist_text)
                    except ValueError:
                        pass

                result.stores.append(store)

        # Try __NEXT_DATA__ or embedded JSON for SPA pages
        for script in soup.select("script"):
            script_text = script.string or ""
            if "__NEXT_DATA__" in script_text:
                try:
                    json_match = re.search(
                        r'__NEXT_DATA__\s*=\s*({.+?})\s*(?:;|</script>)',
                        script_text,
                        re.DOTALL,
                    )
                    if json_match:
                        next_data = json.loads(json_match.group(1))
                        props = next_data.get("props", {}).get("pageProps", {})
                        if props and not result.stores:
                            api_result = self._parse_api_response(props, original_url)
                            if api_result.stores:
                                return api_result
                except (json.JSONDecodeError, KeyError):
                    pass
            elif not result.stores and "stores" in script_text.lower():
                try:
                    json_match = re.search(r'({.*"stores".*})', script_text, re.DOTALL)
                    if json_match:
                        data = json.loads(json_match.group(1))
                        if "stores" in data:
                            for s in data["stores"]:
                                store = StoreInventory(
                                    store_name=s.get("name", ""),
                                    address=s.get("address", ""),
                                    quantity=s.get("quantity", 0),
                                    in_store_price=s.get("price"),
                                    distance_miles=s.get("distance"),
                                    percent_off=s.get("percentOff"),
                                )
                                result.stores.append(store)
                except (json.JSONDecodeError, KeyError):
                    pass

        result.total_stores_checked = len(result.stores)
        self._detect_retailer(result, original_url)
        return result

    def _detect_retailer(self, result: MapperResult, url: str):
        url_lower = url.lower()
        retailer_map = {
            "target": "Target",
            "walmart": "Walmart",
            "homedepot": "Home Depot",
            "/hd": "Home Depot",
            "lowes": "Lowe's",
            "bestbuy": "Best Buy",
            "/dg": "Dollar General",
            "sams": "Sam's Club",
            "costco": "Costco",
            "/tsc": "Tractor Supply",
            "gamestop": "GameStop",
            "/bjs": "BJ's",
        }
        for pattern, name in retailer_map.items():
            if pattern in url_lower:
                result.retailer = name
                return

    async def check_inventory_api(self, retailer: str, sku: str) -> MapperResult:
        retailer_slug = {
            "Home Depot": "hd",
            "Walmart": "walmart",
            "Target": "target",
            "Lowe's": "lowes",
            "Best Buy": "bestbuy",
            "Dollar General": "dg",
            "Sam's Club": "sams",
            "Costco": "costco",
            "Tractor Supply": "tsc",
            "GameStop": "gamestop",
            "BJ's": "bjs",
        }.get(retailer, retailer.lower())

        url = f"{self.base_url}/inventory-checker/{retailer_slug}?sku={sku}"
        return await self.check_inventory(url)

    async def close(self):
        if self.session:
            await self.session.close()


def yarl_url(url_str: str):
    """Create a yarl.URL for aiohttp cookie jar context."""
    from yarl import URL
    return URL(url_str)


def _int(value) -> int:
    """Safely convert a value to int."""
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0
