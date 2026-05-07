"""
Deal Parser - Extracts deal information from Discord messages.

Handles all the different post formats seen in Profit Lounge:
- Staff structured posts (product name, MSRP, sale price, mapper link)
- Member freeform posts (links, price mentions, screenshots)
- Card/Pokemon drops (SKU, OID, MSRP, stock)
- Forwarded deals from other channels
"""

import re
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass
class ParsedDeal:
    """Represents a parsed deal from a Discord message."""
    # Source info
    message_id: str = ""
    channel_id: str = ""
    channel_name: str = ""
    author: str = ""
    timestamp: datetime = field(default_factory=datetime.now)

    # Product info
    product_name: str = ""
    brand: str = ""
    retailer: str = ""

    # Pricing
    sale_price: Optional[float] = None
    msrp: Optional[float] = None
    percent_off: Optional[float] = None

    # Identifiers
    sku: Optional[str] = None
    upc: Optional[str] = None
    oid: Optional[str] = None

    # Links
    profit_mapper_url: Optional[str] = None
    product_url: Optional[str] = None
    barcode_url: Optional[str] = None

    # Stock info from the post itself (before we check mapper)
    stock_hint: Optional[str] = None

    # Raw content for context
    raw_content: str = ""

    # Scoring metadata
    hot_keyword_matches: list = field(default_factory=list)

    @property
    def has_mapper_link(self) -> bool:
        return self.profit_mapper_url is not None

    @property
    def profit_estimate(self) -> Optional[float]:
        if self.sale_price is not None and self.msrp is not None:
            return self.msrp - self.sale_price
        return None

    @property
    def calculated_percent_off(self) -> Optional[float]:
        if self.percent_off is not None:
            return self.percent_off
        if self.sale_price is not None and self.msrp is not None and self.msrp > 0:
            return ((self.msrp - self.sale_price) / self.msrp) * 100
        return None


# Regex patterns for extracting deal info
PATTERNS = {
    # Profit Mapper inventory checker links
    "mapper_link": re.compile(
        r'https?://profit-mapper\.com/inventory-checker/(\w+)\?(?:sku|upc)=(\d+)',
        re.IGNORECASE
    ),
    # Barcode.live links
    "barcode_link": re.compile(
        r'https?://barcode\.live/\?upc=(\d+)',
        re.IGNORECASE
    ),
    # Profit Mapper shopping list links
    "mapper_shopping_list": re.compile(
        r'https?://profit-mapper\.com/?\?shoppingListId=(\d+)',
        re.IGNORECASE
    ),
    # General profit mapper links
    "mapper_general": re.compile(
        r'https?://profit-mapper\.com/[^\s<>]+',
        re.IGNORECASE
    ),
    # Price patterns: "$149.99", "as low as $4", "$0.01"
    "price": re.compile(
        r'\$(\d{1,5}(?:\.\d{2})?)',
    ),
    # "As low as $X" pattern (common in staff deals)
    "as_low_as": re.compile(
        r'as\s+low\s+as\s+\$(\d{1,5}(?:\.\d{2})?)',
        re.IGNORECASE
    ),
    # MSRP patterns: "MSRP: $499.99", "(MSRP: $499.99)", "MSRP $56"
    "msrp": re.compile(
        r'MSRP:?\s*\$(\d{1,5}(?:\.\d{2})?)',
        re.IGNORECASE
    ),
    # Percent off: "30% off", "75% Off", "100% Off"
    "percent_off": re.compile(
        r'(\d{1,3})%\s*off',
        re.IGNORECASE
    ),
    # SKU patterns
    "sku": re.compile(
        r'(?:SKU|sku|Product\s*#?)[:.\s]*(\d{6,15})',
        re.IGNORECASE
    ),
    # UPC patterns
    "upc": re.compile(
        r'(?:UPC|upc)[:.\s]*(\d{10,14})',
        re.IGNORECASE
    ),
    # OID patterns (seen in card drops)
    "oid": re.compile(
        r'OID:\s*([A-F0-9]+)',
        re.IGNORECASE
    ),
    # Stock hints
    "stock": re.compile(
        r'Stock:\s*(\d+[kK]?\+?)',
        re.IGNORECASE
    ),
    # Walmart SKU in URL
    "walmart_sku": re.compile(
        r'walmart\.com/ip/\w+/(\d+)',
        re.IGNORECASE
    ),
    # Target SKU in URL
    "target_sku_url": re.compile(
        r'target\.com/.+?/A-(\d+)',
        re.IGNORECASE
    ),
    # Home Depot SKU in URL
    "hd_sku_url": re.compile(
        r'homedepot\.com/.+?/(\d{9})',
        re.IGNORECASE
    ),
    # Best Buy SKU in URL
    "bestbuy_sku_url": re.compile(
        r'bestbuy\.com/.+?/(\d{7})',
        re.IGNORECASE
    ),
    # Retailer product URLs
    "product_url": re.compile(
        r'https?://(?:www\.)?(?:walmart\.com|target\.com|homedepot\.com|lowes\.com|bestbuy\.com|amazon\.com|costco\.com|dollargeneralcom|gamestop\.com)[^\s<>]+',
        re.IGNORECASE
    ),
    # Mavely affiliate links (common in Profit Lounge)
    "mavely_link": re.compile(
        r'https?://mavely\.app\.link/\S+',
        re.IGNORECASE
    ),
    # Best Buy creators links
    "bestbuy_creators": re.compile(
        r'https?://bestbuycreators\.\S+',
        re.IGNORECASE
    ),
}

# Retailer detection from channel names
RETAILER_PATTERNS = {
    "walmart": "Walmart",
    "target": "Target",
    "hd": "Home Depot",
    "homedepot": "Home Depot",
    "lowes": "Lowe's",
    "bestbuy": "Best Buy",
    "costco": "Costco",
    "dg": "Dollar General",
    "sams": "Sam's Club",
    "bjs": "BJ's",
    "tsc": "Tractor Supply",
    "popmart": "PopMart",
    "canon": "Canon",
    "nordstrom": "Nordstrom",
    "cricket": "Cricket",
    "napa": "NAPA",
    "gamestop": "GameStop",
}

# Brand detection keywords
BRAND_KEYWORDS = [
    "apple", "dewalt", "milwaukee", "makita", "ryobi", "samsung",
    "lg", "sony", "nintendo", "pokemon", "dyson", "ninja", "philips",
    "delonghi", "google", "nanit", "therabody", "stanley", "yeti",
    "cricut", "husky", "kobalt", "trafficmaster", "style selections",
]


def detect_retailer(channel_name: str) -> str:
    """Detect retailer from channel name."""
    name_lower = channel_name.lower().replace("-", "").replace("_", "")
    for pattern, retailer in RETAILER_PATTERNS.items():
        if pattern in name_lower:
            return retailer
    return "General"


def detect_brand(text: str) -> str:
    """Detect brand from product text."""
    text_lower = text.lower()
    for brand in BRAND_KEYWORDS:
        if brand in text_lower:
            return brand.title()
    return ""


def parse_message(message) -> Optional[ParsedDeal]:
    """
    Parse a Discord message into a ParsedDeal.

    Args:
        message: Discord message object

    Returns:
        ParsedDeal if the message contains deal info, None otherwise
    """
    content = message.content or ""
    # Include embed content
    embed_text = ""
    for embed in message.embeds:
        if embed.title:
            embed_text += f" {embed.title}"
        if embed.description:
            embed_text += f" {embed.description}"

    full_text = f"{content} {embed_text}".strip()

    if not full_text or len(full_text) < 10:
        return None

    deal = ParsedDeal(
        message_id=str(message.id),
        channel_id=str(message.channel.id),
        channel_name=message.channel.name,
        author=str(message.author),
        timestamp=message.created_at,
        raw_content=content,
        retailer=detect_retailer(message.channel.name),
    )

    # Extract Profit Mapper link (the key piece)
    mapper_match = PATTERNS["mapper_link"].search(full_text)
    if mapper_match:
        deal.profit_mapper_url = mapper_match.group(0)
        retailer_from_url = mapper_match.group(1)
        sku_from_url = mapper_match.group(2)
        deal.sku = sku_from_url
        # Update retailer if we got it from URL
        for pattern, name in RETAILER_PATTERNS.items():
            if pattern in retailer_from_url.lower():
                deal.retailer = name
                break
    else:
        # Check for general mapper links
        general_match = PATTERNS["mapper_general"].search(full_text)
        if general_match:
            deal.profit_mapper_url = general_match.group(0)

    # Extract barcode link
    barcode_match = PATTERNS["barcode_link"].search(full_text)
    if barcode_match:
        deal.barcode_url = barcode_match.group(0)
        deal.upc = barcode_match.group(1)

    # Extract prices
    msrp_match = PATTERNS["msrp"].search(full_text)
    if msrp_match:
        deal.msrp = float(msrp_match.group(1))

    # "As low as" price is the sale price
    as_low_match = PATTERNS["as_low_as"].search(full_text)
    if as_low_match:
        deal.sale_price = float(as_low_match.group(1))
    else:
        # Find all prices and try to determine sale vs MSRP
        all_prices = [float(p) for p in PATTERNS["price"].findall(full_text)]
        if all_prices:
            if deal.msrp and len(all_prices) >= 1:
                # The price that's not the MSRP is likely the sale price
                non_msrp = [p for p in all_prices if abs(p - deal.msrp) > 0.01]
                if non_msrp:
                    deal.sale_price = min(non_msrp)
            elif len(all_prices) >= 2:
                # Assume highest is MSRP, lowest is sale
                deal.msrp = deal.msrp or max(all_prices)
                deal.sale_price = min(all_prices)
            elif len(all_prices) == 1 and not deal.msrp:
                deal.sale_price = all_prices[0]

    # Percent off
    pct_match = PATTERNS["percent_off"].search(full_text)
    if pct_match:
        deal.percent_off = float(pct_match.group(1))

    # SKU / UPC / OID
    if not deal.sku:
        sku_match = PATTERNS["sku"].search(full_text)
        if sku_match:
            deal.sku = sku_match.group(1)

    if not deal.upc:
        upc_match = PATTERNS["upc"].search(full_text)
        if upc_match:
            deal.upc = upc_match.group(1)

    oid_match = PATTERNS["oid"].search(full_text)
    if oid_match:
        deal.oid = oid_match.group(1)

    # Stock hints
    stock_match = PATTERNS["stock"].search(full_text)
    if stock_match:
        deal.stock_hint = stock_match.group(1)

    # Product URL
    url_match = PATTERNS["product_url"].search(full_text)
    if url_match:
        deal.product_url = url_match.group(0)

    # Try to extract product name from first line or embed title
    lines = content.strip().split("\n")
    for line in lines:
        line_clean = line.strip()
        # Skip lines that are just links or prices
        if line_clean.startswith("http") or line_clean.startswith("$"):
            continue
        # Skip very short lines or metadata lines
        if len(line_clean) < 5:
            continue
        if any(line_clean.lower().startswith(p) for p in ["msrp", "sku", "upc", "oid", "stock", "inventory checker", "as low as"]):
            continue
        # This is likely the product name
        deal.product_name = line_clean[:200]
        break

    # Fallback to embed title for product name
    if not deal.product_name:
        for embed in message.embeds:
            if embed.title and len(embed.title) > 5:
                deal.product_name = embed.title[:200]
                break

    # Brand detection
    deal.brand = detect_brand(f"{deal.product_name} {full_text}")

    # Only return if we have something useful
    has_price_info = deal.sale_price is not None or deal.percent_off is not None
    has_link = deal.profit_mapper_url is not None or deal.product_url is not None
    has_product = deal.product_name != ""

    if has_price_info or has_link or (has_product and has_link):
        return deal

    return None


def check_hot_keywords(deal: ParsedDeal, hot_keywords: list) -> list:
    """Check if deal content matches any hot keywords."""
    matches = []
    text_lower = deal.raw_content.lower()
    for keyword in hot_keywords:
        if keyword.lower() in text_lower:
            matches.append(keyword)
    deal.hot_keyword_matches = matches
    return matches
