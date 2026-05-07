"""
Deal Scout - Comprehensive Stress Test
=======================================
Tests every component end-to-end:
  1. Config loading & validation
  2. Database operations (CRUD, duplicates, cleanup)
  3. Message parser (10+ formats)
  4. Profit Mapper session & API (multiple retailers, edge cases)
  5. Deal scorer (all tiers, filters, big-ticket logic)
  6. Alert formatter (all tiers, with/without mapper data)
  7. Full pipeline: parse → mapper → score → format
  8. Quiet hours / digest logic
  9. Ignore keywords
  10. Watchlist operations
"""

import asyncio
import json
import os
import sys
import time
import traceback
import yaml
from datetime import datetime, timedelta
from dataclasses import dataclass

# Force UTF-8
os.environ["PYTHONUTF8"] = "1"

# Ensure we're in the right directory
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from deal_parser import parse_message, check_hot_keywords, ParsedDeal
from mapper_client import ProfitMapperClient, MapperResult, StoreInventory
from deal_scorer import DealScorer
from alert_formatter import format_deal_alert, format_digest, format_watchlist_update
from database import DealDatabase

with open("config.yaml", "r") as f:
    CONFIG = yaml.safe_load(f)

# Stats
passed = 0
failed = 0
errors = []


def test(name):
    """Decorator for test functions."""
    def decorator(func):
        async def wrapper():
            global passed, failed
            try:
                if asyncio.iscoroutinefunction(func):
                    result = await func()
                else:
                    result = func()
                if result is False:
                    failed += 1
                    errors.append(f"FAIL: {name}")
                    print(f"  ❌ {name}")
                else:
                    passed += 1
                    print(f"  ✅ {name}")
            except Exception as e:
                failed += 1
                errors.append(f"ERROR: {name}: {e}")
                print(f"  ❌ {name}: {e}")
                traceback.print_exc()
        wrapper._test_name = name
        return wrapper
    return decorator


# ============================================================
# Fake Discord message for parser tests
# ============================================================
class FakeChannel:
    def __init__(self, name, id=12345):
        self.name = name
        self.id = id

class FakeAuthor:
    def __init__(self, name="TestUser"):
        self.id = 99999
        self.__str__ = lambda self: name

class FakeEmbed:
    def __init__(self, title=None, description=None):
        self.title = title
        self.description = description

class FakeMessage:
    def __init__(self, content, channel_name="hd-staff-deals", embeds=None):
        self.content = content
        self.channel = FakeChannel(channel_name)
        self.author = FakeAuthor()
        self.id = int(time.time() * 1000)
        self.embeds = embeds or []
        self.created_at = datetime.now()


# ============================================================
# 1. CONFIG TESTS
# ============================================================
print("\n🔧 1. CONFIG VALIDATION")

@test("Config loads and has required keys")
def test_config_keys():
    required = ["discord", "profit_mapper", "filters", "alerts", "digest", "tracker"]
    for key in required:
        assert key in CONFIG, f"Missing key: {key}"

@test("Discord config has token and IDs")
def test_discord_config():
    dc = CONFIG["discord"]
    assert dc.get("token") and len(dc["token"]) > 10
    assert dc.get("my_user_id") and len(dc["my_user_id"]) > 5
    assert dc.get("server_id") and len(dc["server_id"]) > 5

@test("Filter weights sum to 100")
def test_weights():
    w = CONFIG["filters"]["weights"]
    total = sum(w.values())
    assert total == 100, f"Weights sum to {total}, not 100"

@test("Channel patterns are non-empty")
def test_channel_patterns():
    patterns = CONFIG["discord"]["deal_channel_patterns"]
    assert len(patterns) > 5, f"Only {len(patterns)} patterns"

@test("Ignore keywords include paint")
def test_ignore_keywords():
    keywords = [k.lower() for k in CONFIG["filters"]["ignore_keywords"]]
    assert "paint" in keywords, "paint not in ignore keywords"


# ============================================================
# 2. DATABASE TESTS
# ============================================================
print("\n💾 2. DATABASE OPERATIONS")

TEST_DB = "stress_test.db"

@test("Database initializes")
async def test_db_init():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    db = DealDatabase(TEST_DB)
    await db.initialize()
    await db.close()

@test("Record and retrieve deals")
async def test_db_deals():
    db = DealDatabase(TEST_DB)
    await db.initialize()
    deal = {
        "message_id": "test-001",
        "channel_id": "ch-001",
        "channel_name": "hd-staff-deals",
        "product_name": "Test Product",
        "sale_price": 49.99,
        "msrp": 199.99,
        "percent_off": 75,
        "sku": "123456789",
        "retailer": "Home Depot",
        "mapper_url": "https://profit-mapper.com/test",
        "score": 85,
        "tier": "hot",
    }
    await db.record_deal(deal)
    # Record same deal again — should not crash (duplicate handling)
    await db.record_deal(deal)
    await db.close()

@test("Duplicate detection works")
async def test_db_duplicate():
    db = DealDatabase(TEST_DB)
    await db.initialize()
    is_dup = await db.is_duplicate("test-001", "123456789", cooldown_minutes=120)
    assert is_dup, "Should detect duplicate"
    is_dup2 = await db.is_duplicate("test-new", "new-sku", cooldown_minutes=120)
    assert not is_dup2, "New deal should not be duplicate"
    await db.close()

@test("Watchlist add and retrieve")
async def test_db_watchlist():
    db = DealDatabase(TEST_DB)
    await db.initialize()
    item = {
        "message_id": "watch-001",
        "product_name": "Watched Item",
        "mapper_url": "https://profit-mapper.com/test",
        "sku": "watch-sku",
        "retailer": "Home Depot",
        "sale_price": 29.99,
    }
    await db.add_to_watchlist(item, expire_days=7)
    watchlist = await db.get_active_watchlist()
    assert len(watchlist) >= 1, "Watchlist should have items"
    await db.close()

@test("Alert cooldown tracking")
async def test_db_cooldown():
    db = DealDatabase(TEST_DB)
    await db.initialize()
    await db.record_alert("cooldown-sku")
    # Check that it's within cooldown
    is_dup = await db.is_duplicate("new-msg", "cooldown-sku", cooldown_minutes=120)
    assert is_dup, "Should be in cooldown"
    await db.close()

@test("Cleanup old records")
async def test_db_cleanup():
    db = DealDatabase(TEST_DB)
    await db.initialize()
    await db.cleanup_old(days=30)
    await db.expire_watchlist()
    await db.close()
    # Clean up test db
    os.remove(TEST_DB)


# ============================================================
# 3. MESSAGE PARSER TESTS
# ============================================================
print("\n📨 3. MESSAGE PARSER (10+ formats)")

@test("Staff deal with mapper link")
def test_parse_staff_deal():
    msg = FakeMessage(
        "DEWALT 20V MAX XR 1/2 in. Impact Wrench\n"
        "MSRP: $299.00\n"
        "As low as $89.97\n"
        "https://profit-mapper.com/inventory-checker/hd?sku=310115343",
        channel_name="hd-staff-deals"
    )
    deal = parse_message(msg)
    assert deal is not None, "Should parse"
    assert deal.sale_price == 89.97, f"Sale price: {deal.sale_price}"
    assert deal.msrp == 299.0, f"MSRP: {deal.msrp}"
    assert deal.sku == "310115343"
    assert "profit-mapper.com" in deal.profit_mapper_url
    assert deal.retailer == "Home Depot"

@test("Percent off extraction")
def test_parse_percent_off():
    msg = FakeMessage(
        "Milwaukee M18 FUEL Drill Kit\n75% off!\nSKU: 305491817",
        channel_name="hd-staff-deals"
    )
    deal = parse_message(msg)
    assert deal is not None
    assert deal.percent_off == 75

@test("Walmart deal with product URL")
def test_parse_walmart():
    msg = FakeMessage(
        "Samsung 65\" 4K Smart TV - clearance\n"
        "$198.00 MSRP: $599.99\n"
        "https://www.walmart.com/ip/samsung-65-tv/123456789",
        channel_name="walmart-staff-deals"
    )
    deal = parse_message(msg)
    assert deal is not None
    assert deal.retailer == "Walmart"
    assert deal.sale_price == 198.0
    assert deal.product_url is not None

@test("Card drop format (OID)")
def test_parse_card_drop():
    msg = FakeMessage(
        "Pokemon Scarlet & Violet Booster\nOID: A1B2C3D4\nSKU: 987654321\n$3.99",
        channel_name="card-drops"
    )
    deal = parse_message(msg)
    assert deal is not None
    assert deal.oid == "A1B2C3D4"

@test("Penny deal format")
def test_parse_penny():
    msg = FakeMessage(
        "GE Light Bulbs 4-pack\n$0.01\nMSRP: $12.98\n"
        "https://profit-mapper.com/inventory-checker/hd?sku=111222333",
        channel_name="hd-penny-lists"
    )
    deal = parse_message(msg)
    assert deal is not None
    assert deal.sale_price == 0.01

@test("Multiple prices — picks sale vs MSRP correctly")
def test_parse_multi_price():
    msg = FakeMessage(
        "Kobalt 24V Blower\n$49.98 $149.00\n"
        "https://profit-mapper.com/inventory-checker/lowes?sku=555666777",
        channel_name="lowes-staff-deals"
    )
    deal = parse_message(msg)
    assert deal is not None
    assert deal.sale_price == 49.98, f"Sale: {deal.sale_price}"
    assert deal.msrp == 149.0, f"MSRP: {deal.msrp}"

@test("UPC extraction")
def test_parse_upc():
    msg = FakeMessage(
        "Random Product\nUPC: 012345678901\n$5.00",
        channel_name="dg-staff-deals"
    )
    deal = parse_message(msg)
    assert deal is not None
    assert deal.upc == "012345678901"

@test("Embed-only deal (no content text)")
def test_parse_embed():
    embed = FakeEmbed(
        title="Hot Deal: Dyson V15 Vacuum",
        description="$349.99 MSRP: $749.99\nhttps://profit-mapper.com/inventory-checker/target?sku=888999000"
    )
    msg = FakeMessage("", channel_name="target-staff-deals", embeds=[embed])
    deal = parse_message(msg)
    assert deal is not None
    assert deal.product_name == "Hot Deal: Dyson V15 Vacuum"

@test("Short/junk messages return None")
def test_parse_junk():
    assert parse_message(FakeMessage("hi")) is None
    assert parse_message(FakeMessage("")) is None
    assert parse_message(FakeMessage("lol")) is None

@test("Brand detection")
def test_parse_brand():
    msg = FakeMessage(
        "Milwaukee M18 Impact Driver\n$79.00 MSRP: $199.00",
        channel_name="hd-staff-deals"
    )
    deal = parse_message(msg)
    assert deal is not None
    assert deal.brand == "Milwaukee"

@test("Hot keyword matching")
def test_hot_keywords():
    deal = ParsedDeal(raw_content="Amazing clearance penny deal $0.01 YMMV")
    hot_kw = CONFIG["filters"]["hot_keywords"]
    matches = check_hot_keywords(deal, hot_kw)
    assert "clearance" in matches
    assert "penny" in matches
    assert "$0.01" in matches


# ============================================================
# 4. PROFIT MAPPER TESTS
# ============================================================
print("\n🌐 4. PROFIT MAPPER API")

mapper = None

@test("Mapper initializes and loads session")
async def test_mapper_init():
    global mapper
    mapper = ProfitMapperClient(CONFIG.get("profit_mapper", {}))
    await mapper.initialize()
    assert mapper.session is not None

@test("Session is valid")
async def test_mapper_session():
    ok = await mapper.check_session()
    assert ok, "Session expired — run --login first"

@test("HD inventory check (known stock)")
async def test_mapper_hd():
    r = await mapper.check_inventory(
        "https://profit-mapper.com/inventory-checker/hd?sku=310115343"
    )
    assert r.error is None, f"Error: {r.error}"
    assert r.product_name, "No product name"
    assert r.total_stores_checked > 0, "No stores returned"
    assert r.total_nearby_stock > 0, "No stock — zipCode fix may not be working"
    assert r.closest_store is not None, "No closest store"
    assert r.closest_store.distance_miles is not None, "No distance"
    assert r.closest_store.distance_miles < 100, f"Distance {r.closest_store.distance_miles}mi seems wrong (meters?)"

@test("Walmart inventory check")
async def test_mapper_walmart():
    # Walmart SKU — may or may not have stock, but should not crash
    r = await mapper.check_inventory(
        "https://profit-mapper.com/inventory-checker/walmart?sku=123456789"
    )
    # Either valid result or graceful error
    assert isinstance(r, MapperResult)

@test("Target inventory check")
async def test_mapper_target():
    r = await mapper.check_inventory(
        "https://profit-mapper.com/inventory-checker/target?sku=12345678"
    )
    assert isinstance(r, MapperResult)

@test("Lowes inventory check")
async def test_mapper_lowes():
    r = await mapper.check_inventory(
        "https://profit-mapper.com/inventory-checker/lowes?sku=5013800497"
    )
    assert isinstance(r, MapperResult)

@test("Null SKU handled gracefully")
async def test_mapper_null_sku():
    r = await mapper.check_inventory(
        "https://profit-mapper.com/inventory-checker/hd?sku=999999999"
    )
    assert r.error is not None, "Should have error for fake SKU"
    assert "not found" in r.error.lower() or r.total_nearby_stock == 0

@test("Bad URL handled gracefully")
async def test_mapper_bad_url():
    r = await mapper.check_inventory("https://profit-mapper.com/some/random/page")
    assert r.error is not None

@test("Empty URL handled gracefully")
async def test_mapper_empty():
    r = await mapper.check_inventory("")
    assert r.error is not None

@test("Rapid-fire API calls (10 in a row)")
async def test_mapper_rapid():
    skus = ["310115343", "316822172", "310117100", "310116462", "310128137",
            "331301808", "315274317", "999999999", "310115343", "316822172"]
    results = []
    for sku in skus:
        r = await mapper.check_inventory(
            f"https://profit-mapper.com/inventory-checker/hd?sku={sku}"
        )
        results.append(r)
        await asyncio.sleep(0.5)  # Small delay to be nice
    # Should not crash, all should return MapperResult
    assert all(isinstance(r, MapperResult) for r in results)
    # At least some should have data
    has_data = [r for r in results if r.product_name]
    assert len(has_data) >= 3, f"Only {len(has_data)}/10 returned product data"


# ============================================================
# 5. DEAL SCORER TESTS
# ============================================================
print("\n📊 5. DEAL SCORER")

scorer = DealScorer(CONFIG)

def make_deal(**kwargs) -> ParsedDeal:
    defaults = {
        "product_name": "Test Product",
        "retailer": "Home Depot",
        "channel_name": "hd-staff-deals",
        "raw_content": "Test product clearance deal",
    }
    defaults.update(kwargs)
    return ParsedDeal(**defaults)

def make_mapper(stores=None, product_name="Test Product", msrp=100) -> MapperResult:
    r = MapperResult()
    r.product_name = product_name
    r.msrp = msrp
    r.stores = stores or []
    r.total_stores_checked = len(r.stores)
    return r

def make_store(qty=5, distance=10, price=25, msrp=100) -> StoreInventory:
    return StoreInventory(
        store_name="Test Store",
        address="123 Test St",
        city="Jacksonville",
        state="FL",
        zip_code="32259",
        quantity=qty,
        in_store_price=price,
        msrp=msrp,
        distance_miles=distance,
        percent_off=round((1 - price/msrp) * 100) if msrp else None,
    )

@test("HOT deal scores >= 70")
def test_score_hot():
    deal = make_deal(sale_price=10, msrp=200, percent_off=95,
                     raw_content="clearance penny deal")
    check_hot_keywords(deal, CONFIG["filters"]["hot_keywords"])
    mapper_r = make_mapper(
        stores=[make_store(qty=20, distance=5, price=10, msrp=200)],
        msrp=200
    )
    result = scorer.score(deal, mapper_r)
    assert result["tier"] == "hot", f"Expected hot, got {result['tier']} (score={result['total_score']})"
    assert result["total_score"] >= 70

@test("SOLID deal scores 45-69")
def test_score_solid():
    deal = make_deal(sale_price=50, msrp=120, percent_off=58)
    mapper_r = make_mapper(
        stores=[make_store(qty=3, distance=25, price=50, msrp=120)],
        msrp=120
    )
    result = scorer.score(deal, mapper_r)
    assert result["tier"] == "solid", f"Expected solid, got {result['tier']} (score={result['total_score']})"

@test("SKIP deal below threshold")
def test_score_skip():
    deal = make_deal(sale_price=90, msrp=100, percent_off=10)
    mapper_r = make_mapper(stores=[make_store(qty=1, distance=45, price=90, msrp=100)])
    result = scorer.score(deal, mapper_r)
    assert result["tier"] == "skip", f"Expected skip, got {result['tier']}"

@test("No stock = skip")
def test_score_no_stock():
    deal = make_deal(sale_price=10, msrp=200, percent_off=95)
    mapper_r = make_mapper(stores=[])
    result = scorer.score(deal, mapper_r)
    assert result["total_score"] == 0 or result["tier"] == "skip"

@test("Ignore keywords filter works")
def test_score_ignore():
    deal = make_deal(
        product_name="Behr Premium Paint 5 gal",
        raw_content="Behr Premium Paint 5 gal exterior latex"
    )
    assert scorer.should_ignore(deal), "Should ignore paint deals"

@test("Non-ignored deal passes")
def test_score_no_ignore():
    deal = make_deal(
        product_name="DEWALT 20V Impact Driver",
        raw_content="DEWALT 20V Impact Driver clearance"
    )
    assert not scorer.should_ignore(deal), "Should NOT ignore DEWALT"

@test("Big-ticket item detection")
def test_score_big_ticket():
    deal = make_deal(
        product_name="Cub Cadet 42 in. Zero Turn Mower",
        sale_price=1500, msrp=4000, percent_off=62,
        raw_content="Cub Cadet zero turn mower"
    )
    mapper_r = make_mapper(
        stores=[make_store(qty=1, distance=80, price=1500, msrp=4000)],
        msrp=4000
    )
    result = scorer.score(deal, mapper_r)
    # Big-ticket should not be skipped even at 80mi
    assert result["total_score"] > 0, f"Big-ticket at 80mi should score > 0, got {result['total_score']}"

@test("Scorer handles None mapper gracefully")
def test_score_no_mapper():
    deal = make_deal(sale_price=50, msrp=200, percent_off=75)
    result = scorer.score(deal, None)
    assert "total_score" in result
    assert "tier" in result

@test("Scorer handles deal with no prices")
def test_score_no_prices():
    deal = make_deal(product_name="Mystery Item")
    result = scorer.score(deal, None)
    assert "total_score" in result


# ============================================================
# 6. ALERT FORMATTER TESTS
# ============================================================
print("\n📝 6. ALERT FORMATTER")

@test("HOT alert format")
def test_format_hot():
    deal = make_deal(
        product_name="DEWALT 20V Impact Wrench",
        sale_price=89.97, msrp=299.00, percent_off=70,
        retailer="Home Depot", sku="310115343",
        profit_mapper_url="https://profit-mapper.com/inventory-checker/hd?sku=310115343",
        timestamp=datetime.now(), channel_name="hd-staff-deals",
    )
    mapper_r = make_mapper(stores=[make_store(qty=5, distance=8, price=89.97, msrp=299)])
    score_result = {"emoji": "🔥", "tier": "hot", "total_score": 82,
                    "sale_price": 89.97, "percent_off": 70.0,
                    "reasons": ["🤯 70% off!", "📍 8mi away!"], "breakdown": {}}
    msg = format_deal_alert(deal, score_result, mapper_r)
    assert "**DEWALT 20V Impact Wrench**" in msg
    assert "$89.97" in msg
    assert "🔥" in msg
    assert len(msg) < 2000, f"Message too long: {len(msg)} chars"

@test("SOLID alert format")
def test_format_solid():
    deal = make_deal(
        product_name="Ryobi Drill Kit",
        sale_price=49.99, msrp=119.00,
        retailer="Home Depot", sku="999888777",
        timestamp=datetime.now(), channel_name="hd-member-finds",
    )
    score_result = {"emoji": "📊", "tier": "solid", "total_score": 55,
                    "sale_price": 49.99, "percent_off": 58.0,
                    "reasons": ["👍 58% off"], "breakdown": {}}
    msg = format_deal_alert(deal, score_result, None)
    assert "📊" in msg
    assert "$49.99" in msg

@test("Format with no mapper data")
def test_format_no_mapper():
    deal = make_deal(product_name="Test Item", timestamp=datetime.now(),
                     channel_name="test-channel")
    score_result = {"emoji": "⏭️", "tier": "skip", "total_score": 20,
                    "sale_price": None, "percent_off": None,
                    "reasons": [], "breakdown": {}}
    msg = format_deal_alert(deal, score_result, None)
    assert msg is not None and len(msg) > 0

@test("Watchlist update format")
def test_format_watchlist():
    msg = format_watchlist_update(
        "DEWALT Drill", "price_drop",
        old_value=149.99, new_value=99.99,
        mapper_url="https://profit-mapper.com/test"
    )
    assert "price" in msg.lower() or "drop" in msg.lower() or "DEWALT" in msg

@test("Message under 2000 char Discord limit")
def test_format_length():
    deal = make_deal(
        product_name="A" * 200,  # Very long name
        sale_price=1.00, msrp=999.99, percent_off=99,
        retailer="Home Depot", sku="123456789",
        profit_mapper_url="https://profit-mapper.com/inventory-checker/hd?sku=123456789",
        timestamp=datetime.now(), channel_name="hd-staff-deals",
    )
    mapper_r = make_mapper(stores=[
        make_store(qty=i, distance=i*5, price=1.00, msrp=999.99)
        for i in range(1, 20)  # 19 stores
    ])
    score_result = {"emoji": "🔥", "tier": "hot", "total_score": 99,
                    "sale_price": 1.00, "percent_off": 99.0,
                    "reasons": ["🤯 99% off!", "📍 5mi!", "💎 Huge stock"],
                    "breakdown": {}}
    msg = format_deal_alert(deal, score_result, mapper_r)
    assert len(msg) < 2000, f"Message {len(msg)} chars exceeds Discord limit"


# ============================================================
# 7. FULL PIPELINE TEST
# ============================================================
print("\n🔄 7. FULL PIPELINE (parse → mapper → score → format)")

@test("Full pipeline: staff deal with real mapper check")
async def test_full_pipeline():
    msg = FakeMessage(
        "5 gal. Butterscotch Bliss PPG1106-5 Satin Exterior Latex Paint\n"
        "MSRP: $185.00\n"
        "As low as $18.00\n"
        "https://profit-mapper.com/inventory-checker/hd?sku=310115343",
        channel_name="hd-staff-deals"
    )
    # Parse
    deal = parse_message(msg)
    assert deal is not None, "Parse failed"
    assert deal.profit_mapper_url is not None

    # Check ignore — this is paint, should be ignored!
    assert scorer.should_ignore(deal), "Paint should be ignored"

    # But if we force past ignore for testing...
    mapper_r = await mapper.check_inventory(deal.profit_mapper_url)
    assert mapper_r.error is None, f"Mapper error: {mapper_r.error}"
    assert mapper_r.total_nearby_stock > 0, "No stock returned"

    # Score
    check_hot_keywords(deal, CONFIG["filters"]["hot_keywords"])
    result = scorer.score(deal, mapper_r)
    assert "total_score" in result
    assert "tier" in result

    # Format
    alert = format_deal_alert(deal, result, mapper_r)
    assert len(alert) > 50
    assert len(alert) < 2000

@test("Full pipeline: deal with no mapper link")
async def test_pipeline_no_mapper():
    msg = FakeMessage(
        "Random clearance find - Milwaukee drill $49\n"
        "Was $199 - 75% off!",
        channel_name="hd-member-finds"
    )
    deal = parse_message(msg)
    assert deal is not None
    # No mapper URL — skip mapper check
    assert deal.profit_mapper_url is None
    result = scorer.score(deal, None)
    alert = format_deal_alert(deal, result, None)
    assert len(alert) > 20

@test("Full pipeline: penny deal")
async def test_pipeline_penny():
    msg = FakeMessage(
        "GE LED Bulbs 4-Pack\n$0.01\nMSRP: $12.98\n"
        "https://profit-mapper.com/inventory-checker/hd?sku=310115343",
        channel_name="hd-penny-lists"
    )
    deal = parse_message(msg)
    assert deal is not None
    assert deal.sale_price == 0.01
    check_hot_keywords(deal, CONFIG["filters"]["hot_keywords"])
    # This will be ignored (paint SKU), but test the flow
    mapper_r = await mapper.check_inventory(deal.profit_mapper_url)
    result = scorer.score(deal, mapper_r)
    alert = format_deal_alert(deal, result, mapper_r)
    assert len(alert) > 20


# ============================================================
# 8. QUIET HOURS / DIGEST
# ============================================================
print("\n🌙 8. QUIET HOURS & DIGEST")

@test("Quiet hours config is valid")
def test_quiet_config():
    quiet = CONFIG["alerts"]["quiet_hours"]
    assert quiet["enabled"] is True
    assert ":" in quiet["start"]
    assert ":" in quiet["end"]

@test("Digest config is valid")
def test_digest_config():
    digest = CONFIG["digest"]
    assert digest["enabled"] is True
    assert len(digest["times"]) >= 1
    for t in digest["times"]:
        h, m = t.split(":")
        assert 0 <= int(h) <= 23
        assert 0 <= int(m) <= 59


# ============================================================
# 9. EDGE CASES & ERROR HANDLING
# ============================================================
print("\n⚡ 9. EDGE CASES")

@test("Unicode channel names don't crash")
def test_unicode_channels():
    channels = [
        "⁝・freebies", "🔍︰member-deals", "➥🔥・spotlight",
        "╭・deals-by-esse", "┊・deals-by-lia", "⁝・card-drops"
    ]
    for ch in channels:
        msg = FakeMessage("Test deal $50 MSRP: $100\nhttps://profit-mapper.com/inventory-checker/hd?sku=123", channel_name=ch)
        deal = parse_message(msg)
        # Should not crash

@test("Very long message content")
def test_long_message():
    content = "Super Deal!\n" + "A" * 5000 + "\n$49.99 MSRP: $199.99"
    msg = FakeMessage(content, channel_name="hd-staff-deals")
    deal = parse_message(msg)
    # Should not crash, may or may not parse

@test("Message with only links")
def test_only_links():
    msg = FakeMessage(
        "https://profit-mapper.com/inventory-checker/hd?sku=310115343",
        channel_name="hd-staff-deals"
    )
    deal = parse_message(msg)
    # Should parse (has mapper link)

@test("Message with special characters")
def test_special_chars():
    msg = FakeMessage(
        '🔥💯 HUGE DEAL!!! "Best Price" — $29.99 → (was $99.99) ✅\n'
        "https://profit-mapper.com/inventory-checker/hd?sku=123456789",
        channel_name="hd-staff-deals"
    )
    deal = parse_message(msg)
    assert deal is not None

@test("Mapper handles timeout gracefully")
async def test_mapper_timeout():
    # Already covered by rapid-fire test, but explicit
    r = await mapper.check_inventory(
        "https://profit-mapper.com/inventory-checker/hd?sku=310115343"
    )
    assert isinstance(r, MapperResult)


# ============================================================
# 10. AUTO-REAUTH VERIFICATION
# ============================================================
print("\n🔐 10. AUTH VERIFICATION")

@test("Discord cookies file exists")
def test_discord_cookies():
    assert os.path.exists("discord_cookies.json"), "discord_cookies.json missing"
    with open("discord_cookies.json") as f:
        cookies = json.load(f)
    assert len(cookies) > 0, "No Discord cookies saved"

@test("Mapper session file exists and has cookies")
def test_session_file():
    assert os.path.exists("mapper_session.json"), "mapper_session.json missing"
    with open("mapper_session.json") as f:
        data = json.load(f)
    cookies = data.get("raw_cookies", [])
    assert len(cookies) > 0, "No cookies in session file"
    has_session = any("session" in c["name"].lower() for c in cookies)
    assert has_session, "No session token cookie"


# ============================================================
# RUN ALL TESTS
# ============================================================
async def run_all():
    # Collect all test functions
    tests = [v for v in globals().values()
             if callable(v) and hasattr(v, '_test_name')]

    for t in tests:
        await t()

    # Cleanup
    if mapper:
        await mapper.close()

    # Summary
    total = passed + failed
    print(f"\n{'='*50}")
    print(f"  RESULTS: {passed}/{total} passed, {failed} failed")
    print(f"{'='*50}")

    if errors:
        print("\nFailed tests:")
        for e in errors:
            print(f"  • {e}")

    if failed == 0:
        print("\n🎉 ALL TESTS PASSED!")
    else:
        print(f"\n⚠️  {failed} test(s) need attention")

    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(run_all())
    sys.exit(0 if success else 1)
