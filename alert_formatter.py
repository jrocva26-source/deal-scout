"""
Alert Formatter - Creates clean, scannable DM messages for deal alerts.
"""

from datetime import datetime
from typing import Optional
from deal_parser import ParsedDeal
from mapper_client import MapperResult


def format_deal_alert(
    deal: ParsedDeal,
    score_result: dict,
    mapper: Optional[MapperResult] = None,
) -> str:
    """
    Format a deal alert as a Discord DM message.

    Returns a clean, scannable message string.
    """
    emoji = score_result["emoji"]
    tier = score_result["tier"]
    score = score_result["total_score"]
    sale_price = score_result.get("sale_price")
    pct_off = score_result.get("percent_off")

    lines = []

    # Header line with tier emoji and product name
    name = deal.product_name or "Unknown Product"
    if len(name) > 80:
        name = name[:77] + "..."
    lines.append(f"{emoji} **{name}**")

    # Price line
    price_parts = []
    if sale_price is not None:
        price_parts.append(f"**${sale_price:.2f}**")
    if deal.msrp:
        price_parts.append(f"~~${deal.msrp:.2f}~~")
    if pct_off is not None:
        price_parts.append(f"**{pct_off:.0f}% off**")
    if price_parts:
        lines.append(" · ".join(price_parts))

    # Retailer and source
    source_parts = []
    if deal.retailer and deal.retailer != "General":
        source_parts.append(deal.retailer)
    if deal.sku:
        source_parts.append(f"SKU {deal.sku}")
    if source_parts:
        lines.append(" · ".join(source_parts))

    # Stock/location info from mapper
    if mapper and mapper.has_local_stock:
        closest = mapper.closest_store
        total = mapper.total_nearby_stock
        store_count = len([s for s in mapper.stores if s.quantity > 0])

        stock_line = f"📍 **{total} in stock**"
        if store_count > 1:
            stock_line += f" across {store_count} stores"

        if closest:
            name_short = closest.store_name
            if closest.distance_miles is not None:
                stock_line += f" — {name_short} ({closest.distance_miles:.0f}mi)"
            elif closest.address:
                stock_line += f" — {name_short}"

        lines.append(stock_line)

        # Aisle/bay if available
        if closest and closest.aisle_bay:
            lines.append(f"📍 Aisle: {closest.aisle_bay}")
    elif mapper and not mapper.has_local_stock:
        lines.append("❌ No stock within range")

    # Mapper link
    if deal.profit_mapper_url:
        lines.append(f"🔗 [Check Mapper]({deal.profit_mapper_url})")
    elif deal.product_url:
        lines.append(f"🔗 [Product Link]({deal.product_url})")

    # Source channel and time
    time_str = deal.timestamp.strftime("%I:%M %p").lstrip("0") if deal.timestamp else "just now"
    lines.append(f"⏰ #{deal.channel_name} · {time_str}")

    # Scoring reasons (top 2)
    reasons = score_result.get("reasons", [])
    if reasons:
        lines.append(f"{'  '.join(reasons[:2])}")

    # Score badge
    lines.append(f"`Score: {score}/100`")

    # Pin hint
    lines.append(f"React 📌 to track this deal")

    return "\n".join(lines)


def format_digest(deals: list, period_label: str = "today") -> str:
    """
    Format a digest summary of recent deals.

    Args:
        deals: List of deal dicts from database
        period_label: Human-readable time period

    Returns:
        Formatted digest message
    """
    if not deals:
        return f"📋 **Deal Digest** — No notable deals {period_label}. Quiet day!"

    lines = []
    lines.append(f"📋 **Deal Digest** — {len(deals)} deals {period_label}")
    lines.append("")

    # Group by tier
    hot = [d for d in deals if d["tier"] == "hot"]
    solid = [d for d in deals if d["tier"] == "solid"]

    if hot:
        lines.append(f"🔥 **HOT DEALS ({len(hot)})**")
        for d in hot[:10]:
            price_str = f"${d['sale_price']:.2f}" if d['sale_price'] else "?"
            pct_str = f"{d['percent_off']:.0f}%" if d['percent_off'] else ""
            name = (d["product_name"] or "Unknown")[:50]
            lines.append(f"  • **{name}** — {price_str} {pct_str} [{d['retailer']}]")
            if d.get("mapper_url"):
                lines.append(f"    🔗 {d['mapper_url']}")
        lines.append("")

    if solid:
        lines.append(f"📊 **SOLID DEALS ({len(solid)})**")
        for d in solid[:10]:
            price_str = f"${d['sale_price']:.2f}" if d['sale_price'] else "?"
            pct_str = f"{d['percent_off']:.0f}%" if d['percent_off'] else ""
            name = (d["product_name"] or "Unknown")[:50]
            lines.append(f"  • {name} — {price_str} {pct_str} [{d['retailer']}]")
        lines.append("")

    lines.append("React 📌 on any live alert to add it to your watchlist")

    return "\n".join(lines)


def format_watchlist_update(
    product_name: str,
    change_type: str,
    old_value=None,
    new_value=None,
    mapper_url: str = None,
) -> str:
    """Format a watchlist change notification."""
    name = (product_name or "Tracked Item")[:60]

    if change_type == "restock":
        msg = f"📌 **Restock Alert!** — {name}\n"
        msg += f"Back in stock! {new_value} units now available nearby"
    elif change_type == "price_drop":
        msg = f"📌 **Price Drop!** — {name}\n"
        msg += f"${old_value:.2f} → **${new_value:.2f}**"
    elif change_type == "low_stock":
        msg = f"📌 **Low Stock Warning** — {name}\n"
        msg += f"Only {new_value} left nearby, was {old_value}"
    elif change_type == "out_of_stock":
        msg = f"📌 **Out of Stock** — {name}\n"
        msg += f"No longer available nearby"
    else:
        msg = f"📌 **Update** — {name}\n{change_type}"

    if mapper_url:
        msg += f"\n🔗 [Check Mapper]({mapper_url})"

    return msg
