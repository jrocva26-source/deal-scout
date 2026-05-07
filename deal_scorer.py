"""
Deal Scorer - Calculates a score for each deal based on your preferences.

Tuned for Joshua's style:
- 55%+ off minimum or skip
- Prefers bulk buys (15 units of flooring at 70% off > 1 random gadget)
- Prefers high-ticket items (bigger margin per unit)
- Skips small flips (2 qty x $20 profit = not worth the drive)

Score range: 0-100
- 🔥 HOT (70+): Big money opportunity, go now
- 📊 SOLID (45-69): Worth checking out
- Below 45: Not worth your time, no alert
"""

import logging
from typing import Optional
from deal_parser import ParsedDeal
from mapper_client import MapperResult

logger = logging.getLogger("deal_scout.scorer")


class DealScorer:

    def __init__(self, config: dict):
        filters = config.get("filters", {})
        weights = filters.get("weights", {})

        self.min_percent_off = filters.get("min_percent_off", 55)
        self.max_distance = filters.get("max_distance_miles", 50)
        self.max_buy_price = filters.get("max_buy_price", 1500)
        self.min_quantity = filters.get("min_quantity", 1)
        self.min_profit_per_unit = filters.get("min_profit_per_unit", 25)
        self.min_total_deal_value = filters.get("min_total_deal_value", 50)

        self.hot_threshold = filters.get("hot_threshold", 70)
        self.solid_threshold = filters.get("solid_threshold", 45)

        self.w_percent_off = weights.get("percent_off", 25)
        self.w_proximity = weights.get("proximity", 20)
        self.w_stock = weights.get("stock_level", 20)
        self.w_deal_value = weights.get("deal_value", 25)
        self.w_keyword = weights.get("keyword_bonus", 10)

        self.hot_keywords = [k.lower() for k in filters.get("hot_keywords", [])]
        self.ignore_keywords = [k.lower() for k in filters.get("ignore_keywords", [])]
        self.preferred_categories = [c.lower() for c in filters.get("preferred_categories", [])]

    def should_ignore(self, deal: ParsedDeal) -> bool:
        text_lower = f"{deal.raw_content} {deal.product_name}".lower()
        for keyword in self.ignore_keywords:
            if keyword in text_lower:
                return True
        return False

    # Big-ticket keywords — if the deal matches these, extend distance limits
    BIG_TICKET_KEYWORDS = [
        "trailer", "mower", "lawn mower", "zero turn", "riding mower",
        "utv", "atv", "side by side", "golf cart", "generator",
        "tractor", "pressure washer", "snow blower", "snowblower",
        "grill", "smoker", "hot tub", "spa", "shed", "pergola",
        "playground", "trampoline", "patio set", "furniture set",
        "refrigerator", "washer", "dryer", "dishwasher", "range",
        "oven", "freezer", "mini split", "ac unit", "water heater",
        "tool chest", "workbench", "table saw", "miter saw",
        "compressor", "welder", "pallet", "bulk",
    ]

    def _estimate_deal_size(self, deal, mapper):
        """
        Estimate how big this deal is to determine if extended distance is worth it.
        Returns: "big" (worth a long drive), "medium", or "small"
        """
        sale_price = self._get_sale_price(deal, mapper)
        msrp = self._get_msrp(deal, mapper)
        text_lower = f"{deal.product_name} {deal.raw_content}".lower()

        # Check for big-ticket keywords
        is_big_ticket_item = any(kw in text_lower for kw in self.BIG_TICKET_KEYWORDS)

        # Calculate profit if we can
        profit_per_unit = None
        total_value = None
        if sale_price is not None and msrp is not None:
            profit_per_unit = msrp - sale_price
            total_stock = mapper.total_nearby_stock if mapper and mapper.has_local_stock else 1
            total_value = profit_per_unit * max(total_stock, 1)

        # Big deal: high margin single item OR big-ticket keyword OR huge total value
        if profit_per_unit and profit_per_unit >= 200:
            return "big"
        if is_big_ticket_item:
            return "big"
        if total_value and total_value >= 500:
            return "big"
        if msrp and msrp >= 500:
            return "big"

        # Medium: decent margin or moderate total
        if profit_per_unit and profit_per_unit >= 75:
            return "medium"
        if total_value and total_value >= 200:
            return "medium"

        return "small"

    def _get_max_distance(self, deal, mapper, pct_off):
        """
        Dynamic distance limit based on deal size.
        Big flips (trailers, mowers, UTVs, $500+ margin) = willing to drive further.
        """
        sale_price = self._get_sale_price(deal, mapper)
        msrp = self._get_msrp(deal, mapper)
        text_lower = f"{deal.product_name} {deal.raw_content}".lower()

        is_big_ticket = any(kw in text_lower for kw in self.BIG_TICKET_KEYWORDS)

        profit_per_unit = None
        if sale_price is not None and msrp is not None:
            profit_per_unit = msrp - sale_price

        # Big-ticket items or huge margins → drive up to 150mi
        if is_big_ticket:
            return 150
        if profit_per_unit and profit_per_unit >= 300:
            return 150
        if profit_per_unit and profit_per_unit >= 150:
            return 100
        if msrp and msrp >= 800 and pct_off and pct_off >= 60:
            return 120

        # Normal deals → standard radius
        return self.max_distance

    def _hard_filter(self, deal, mapper, pct_off):
        """
        Hard filters — absolute dealbreakers. Returns skip reason or None.
        Distance limit scales with deal size (big flips = drive further).
        """
        # Must meet minimum discount
        if pct_off is not None and pct_off < self.min_percent_off:
            return f"Only {pct_off:.0f}% off (need {self.min_percent_off}%+)"

        # Must have local stock (if we checked mapper)
        if mapper and not mapper.has_local_stock:
            return "No stock nearby"

        # Dynamic distance — big-ticket items get extended range
        max_dist = self._get_max_distance(deal, mapper, pct_off)
        if mapper and mapper.has_local_stock:
            closest = mapper.closest_store
            if closest and closest.distance_miles and closest.distance_miles > max_dist:
                return f"Too far ({closest.distance_miles:.0f}mi, limit {max_dist}mi for this deal)"

        # Buy price cap
        sale_price = self._get_sale_price(deal, mapper)
        if sale_price is not None and sale_price > self.max_buy_price:
            return f"${sale_price:.0f} over budget"

        # Skip small flips: low qty + low profit per unit
        if mapper and mapper.has_local_stock and sale_price is not None:
            msrp = self._get_msrp(deal, mapper)
            if msrp and sale_price < msrp:
                profit_per_unit = msrp - sale_price
                total_stock = mapper.total_nearby_stock
                total_value = profit_per_unit * total_stock

                if profit_per_unit < self.min_profit_per_unit and total_value < self.min_total_deal_value:
                    return f"Small flip (${profit_per_unit:.0f}/unit x {total_stock} = ${total_value:.0f} total)"

        return None

    def score(self, deal: ParsedDeal, mapper_result: Optional[MapperResult] = None) -> dict:
        breakdown = {}
        reasons = []

        pct_off = self._get_percent_off(deal, mapper_result)
        sale_price = self._get_sale_price(deal, mapper_result)
        msrp = self._get_msrp(deal, mapper_result)

        # Hard filters first
        skip_reason = self._hard_filter(deal, mapper_result, pct_off)
        if skip_reason:
            return {
                "total_score": 0, "tier": "skip", "emoji": "⏭️",
                "breakdown": {}, "reasons": [skip_reason],
                "skip_reason": skip_reason,
                "percent_off": pct_off, "sale_price": sale_price,
            }

        # --- Percent Off (0 to 25) ---
        if pct_off is not None:
            if pct_off >= 90:
                pct_score = self.w_percent_off
                reasons.append(f"🤯 {pct_off:.0f}% off!")
            elif pct_off >= 75:
                pct_score = self.w_percent_off * 0.85
                reasons.append(f"💰 {pct_off:.0f}% off")
            elif pct_off >= 65:
                pct_score = self.w_percent_off * 0.65
                reasons.append(f"👍 {pct_off:.0f}% off")
            elif pct_off >= self.min_percent_off:
                pct_score = self.w_percent_off * 0.4
            else:
                pct_score = 0
        else:
            pct_score = self.w_percent_off * 0.2
        breakdown["percent_off"] = pct_score

        # --- Proximity (0 to 20) ---
        # Big-ticket items get softer distance penalty
        prox_score = 0
        if mapper_result and mapper_result.has_local_stock:
            closest = mapper_result.closest_store
            max_dist = self._get_max_distance(deal, mapper_result, pct_off)
            if closest and closest.distance_miles is not None:
                dist = closest.distance_miles
                # Score relative to the max distance for this deal type
                ratio = dist / max_dist if max_dist > 0 else 1
                if ratio <= 0.15:        # Very close relative to limit
                    prox_score = self.w_proximity
                    reasons.append(f"📍 {dist:.0f}mi away!")
                elif ratio <= 0.3:
                    prox_score = self.w_proximity * 0.8
                    reasons.append(f"📍 {dist:.0f}mi")
                elif ratio <= 0.5:
                    prox_score = self.w_proximity * 0.55
                    reasons.append(f"📍 {dist:.0f}mi")
                elif ratio <= 0.75:
                    prox_score = self.w_proximity * 0.35
                else:
                    prox_score = self.w_proximity * 0.15
                    if max_dist > self.max_distance:
                        reasons.append(f"📍 {dist:.0f}mi (worth the drive)")
        breakdown["proximity"] = prox_score

        # --- Stock Level (0 to 20) ---
        # Bulk is king. 15 units of flooring > 1 gadget
        stock_score = 0
        if mapper_result and mapper_result.has_local_stock:
            total = mapper_result.total_nearby_stock
            if total >= 20:
                stock_score = self.w_stock
                reasons.append(f"📦 {total} units! Bulk opportunity")
            elif total >= 10:
                stock_score = self.w_stock * 0.85
                reasons.append(f"📦 {total} units available")
            elif total >= 5:
                stock_score = self.w_stock * 0.6
                reasons.append(f"📦 {total} units")
            elif total >= 3:
                stock_score = self.w_stock * 0.4
            elif total >= 1:
                stock_score = self.w_stock * 0.15
        breakdown["stock_level"] = stock_score

        # --- Deal Value (0 to 25) ---
        # Total profit opportunity = margin x qty
        # This is the most important factor for your style
        value_score = 0
        if sale_price is not None and msrp is not None and mapper_result:
            profit_per_unit = msrp - sale_price
            total_stock = mapper_result.total_nearby_stock if mapper_result.has_local_stock else 0
            total_value = profit_per_unit * max(total_stock, 1)

            if total_value >= 500:
                value_score = self.w_deal_value
                reasons.append(f"💎 ~${total_value:.0f} total opportunity")
            elif total_value >= 250:
                value_score = self.w_deal_value * 0.8
                reasons.append(f"💰 ~${total_value:.0f} profit potential")
            elif total_value >= 100:
                value_score = self.w_deal_value * 0.6
                reasons.append(f"💵 ~${total_value:.0f} profit potential")
            elif total_value >= self.min_total_deal_value:
                value_score = self.w_deal_value * 0.35
            else:
                value_score = self.w_deal_value * 0.1

            # High-ticket bonus (big margin per unit)
            if profit_per_unit >= 100:
                value_score = min(self.w_deal_value, value_score * 1.2)
                reasons.append(f"🎯 ${profit_per_unit:.0f}/unit margin")
            elif profit_per_unit >= 50:
                value_score = min(self.w_deal_value, value_score * 1.1)

        elif sale_price is not None and sale_price <= 5:
            # Penny/ultra-cheap — always interesting
            value_score = self.w_deal_value * 0.7
            reasons.append(f"🎯 Only ${sale_price:.2f}")
        breakdown["deal_value"] = value_score

        # --- Keyword Bonus (0 to 10) ---
        keyword_score = 0
        text_lower = deal.raw_content.lower()
        matched = []
        for kw in self.hot_keywords:
            if kw in text_lower:
                matched.append(kw)
        for cat in self.preferred_categories:
            full_text = f"{deal.product_name} {deal.brand} {deal.raw_content}".lower()
            if cat in full_text:
                matched.append(f"[{cat}]")
        if matched:
            bonus_pct = min(len(matched) / 3, 1.0)
            keyword_score = self.w_keyword * bonus_pct
            reasons.append(f"🏷️ {', '.join(matched[:3])}")
        breakdown["keyword_bonus"] = keyword_score

        # --- Total ---
        total = min(100, max(0, sum(breakdown.values())))

        if total >= self.hot_threshold:
            tier, emoji = "hot", "🔥"
        elif total >= self.solid_threshold:
            tier, emoji = "solid", "📊"
        else:
            tier, emoji = "skip", "⏭️"

        return {
            "total_score": round(total),
            "tier": tier,
            "emoji": emoji,
            "breakdown": breakdown,
            "reasons": reasons,
            "percent_off": pct_off,
            "sale_price": sale_price,
        }

    def _get_percent_off(self, deal, mapper):
        if mapper and mapper.stores:
            pcts = [s.percent_off for s in mapper.stores if s.percent_off]
            if pcts:
                return max(pcts)
        if deal.calculated_percent_off is not None:
            return deal.calculated_percent_off
        return deal.percent_off

    def _get_sale_price(self, deal, mapper):
        if mapper and mapper.best_price is not None:
            return mapper.best_price
        return deal.sale_price

    def _get_msrp(self, deal, mapper):
        if mapper and mapper.msrp is not None:
            return mapper.msrp
        return deal.msrp
