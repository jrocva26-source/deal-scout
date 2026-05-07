# 🔍 Deal Scout

Personal deal radar for [Profit Lounge](https://profitlounge.com) Discord. Monitors all deal channels 24/7, checks [Profit Mapper](https://profit-mapper.com) for local inventory, and DMs you only the deals worth grabbing.

Built for retail arbitrage — filters out noise so you never miss a deal while you're out sourcing.

## Features

### 🔴 Live Scout
Watches every deal channel (staff-deals, member-deals, major-deals, flips, card-drops, scav, all retailers). When a deal drops:
1. Parses product name, price, percent off, and the Profit Mapper link from the post
2. Checks Profit Mapper for stock within your configured radius
3. Scores the deal based on discount, proximity, stock level, and estimated profit
4. DMs you instantly if it meets your thresholds

### 📊 Smart Scoring (0-100)
Every deal is scored on five factors:

| Factor | Weight | What it measures |
|--------|--------|-----------------|
| Percent Off | 25% | Higher discount = higher score |
| Deal Value | 25% | Total profit opportunity (margin × quantity) |
| Stock Level | 20% | More stock = bulk opportunity |
| Proximity | 20% | Closer stores score higher |
| Keywords | 10% | Matches your preferred categories/keywords |

- **🔥 HOT (70+)** — Big money, go now
- **📊 SOLID (45-69)** — Worth checking out
- **Below 45** — Filtered out, you never see it

### Hard Filters (instant skip)
- Under your minimum discount threshold → skip
- No stock within range → skip
- Small flips (low qty × low margin) → skip
- Over budget → skip

### 🚛 Dynamic Distance
Standard deals: 50mi radius. Big-ticket items (trailers, mowers, UTVs, generators, appliances, $300+ margin) automatically extend to 100-150mi because the drive is worth it.

### 📋 Deal Digest
Scheduled summary DMs (default: 7am, 12pm, 6pm) with a ranked recap of everything that came through. Never miss a deal even when you're busy.

### 📌 Deal Tracker
React to any alert DM with 📌 to add it to your watchlist. The bot re-checks Profit Mapper periodically and alerts on:
- **Restocks** — was out of stock, now available
- **Price drops** — went down another 10%+
- **Low stock** — running out, grab it now

### Other
- 🌙 **Quiet hours** — no DMs during sleep, queued and sent in the morning
- 🔁 **Dedup & cooldowns** — won't spam you on the same product
- ⚠️ **Session monitoring** — alerts you when Profit Mapper login expires

## What It Does NOT Do

- ❌ Does not post, react, or interact in any server channel
- ❌ Does not forward, copy, or redistribute deal content anywhere
- ❌ Does not scrape or store data for anyone other than the owner
- ❌ Does not bypass any server permissions
- ❌ Does not interact with other members

It only **reads** deal channels and **DMs you** via your own account.

## Setup

### 1. Clone & install
```bash
git clone https://github.com/YOUR_USERNAME/deal-scout.git
cd deal-scout
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure
```bash
cp config.example.yaml config.yaml
```
Edit `config.yaml` with:
- Your Discord token
- Your Discord user ID
- The Profit Lounge server ID
- Your zip code and preferred radius
- Your scoring preferences (min discount, preferred categories, etc.)

### 3. Authenticate with Profit Mapper
```bash
python deal_scout.py --login
```
A browser opens — sign in with Discord. Session is saved locally.

### 4. Run
```bash
python deal_scout.py
```

## Project Structure
```
deal-scout/
├── deal_scout.py        # Main bot — ties everything together
├── deal_parser.py       # Extracts deal info from Discord messages
├── mapper_client.py     # Profit Mapper auth & inventory checking
├── deal_scorer.py       # Scoring engine with dynamic distance
├── database.py          # SQLite for deal history & watchlist
├── alert_formatter.py   # Formats clean DM alerts & digests
├── config.example.yaml  # Example config (copy to config.yaml)
├── requirements.txt     # Python dependencies
└── .gitignore
```

## Alert Format
```
🔥 DEWALT 20V MAX Impact Wrench (Tool Only)
$0.01 · $299.00 · 100% off
Home Depot · SKU 317991357
📍 3 in stock — Yulee (37mi)
🔗 Check Mapper
⏰ #hd-staff-deals · 10:08 AM
🤯 100% off!  🎯 Only $0.01
Score: 92/100
React 📌 to track this deal
```

## Configuration

All settings in `config.yaml`. Key things to tune:

| Setting | Default | Description |
|---------|---------|-------------|
| `filters.min_percent_off` | 55 | Minimum discount to consider |
| `filters.max_distance_miles` | 50 | Standard radius (extends for big-ticket) |
| `filters.min_profit_per_unit` | 25 | Skip small flips under this margin |
| `filters.min_total_deal_value` | 50 | Skip if total opportunity is under this |
| `digest.times` | 7am/12pm/6pm | When to get deal summaries |
| `alerts.quiet_hours` | 11pm-7am | No DMs during these hours |

## License

Personal use. Not affiliated with Profit Lounge or Profit Mapper.
