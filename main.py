# main.py - ONION ALERTS: FULLY AUTOMATIC + NEW + MEDIUM + HIGH PAIRS
import os, asyncio, logging, json, time
from collections import defaultdict, deque
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import aiohttp
from telegram.helpers import escape_markdown

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    exit("ERROR: Add BOT_TOKEN")

FREE_ALERTS = 3
PRICE = 19.99
COMMISSION_RATE = 0.25
YOUR_ADMIN_ID = int(os.getenv("ADMIN_ID", "1319494378"))

WALLETS = {
    "BSC": "0xa11351776d6f483418b73c8e40bc706c93e8b1e1"
}

# GoPlus API (free tier) - supports batch
GOPLUS_API = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={addrs}"

# DexScreener Endpoints
NEW_PAIRS_URL = "https://api.dexscreener.com/latest/dex/new-pairs/{chain}"
SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q={chain}"
PAIR_DETAIL_URL = "https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair_addr}"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === PERSISTENT DATA ===
DATA_FILE = "data.json"
SAVE_INTERVAL = 30  # seconds

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                raw = json.load(f)
                seen_list = raw.get("seen", [])
                now = time.time()
                # Keep only last 24h
                seen_clean = [(a, t) for a, t in seen_list if now - t < 86400]
                last_alerted_clean = {k: v for k, v in raw.get("last_alerted", {}).items() if now - v < 3600}
                return {
                    "tracker": raw.get("tracker", {}),
                    "users": raw.get("users", {}),
                    "seen": seen_clean,
                    "last_alerted": last_alerted_clean,
                }
        except Exception as e:
            logger.error(f"Failed to load data: {e}")
    return {"tracker": {}, "users": {}, "seen": [], "last_alerted": {}}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

# Load data
data = load_data()
tracker = data.get("tracker", {})
users = data.get("users", {})
seen = {a for a, _ in data.get("seen", [])}
last_alerted = data.get("last_alerted", {})
vol_hist = defaultdict(lambda: deque(maxlen=5))  # 5-interval history
goplus_cache = {}  # addr -> (is_safe, timestamp)

# === AUTO-SAVE TASK ===
async def auto_save():
    while True:
        await asyncio.sleep(SAVE_INTERVAL)
        save_data({
            "tracker": tracker,
            "users": users,
            "seen": [(a, time.time()) for a in seen],
            "last_alerted": last_alerted
        })

# === HELPERS ===
def get_dex_url(chain: str, pair_addr: str) -> str:
    chain_slug = "solana" if chain == "SOL" else "bsc"
    return f"https://dexscreener.com/{chain_slug}/{pair_addr}"

def format_alert(chain: str, sym: str, addr: str, liq: float, fdv: float, vol: float, pair_addr: str, reason: str):
    sym_safe = escape_markdown(sym, version=2)
    return (
        f"**ALPHA {chain}** [{reason}]\n"
        f"`{sym_safe}`\n"
        f"**CA:** `{addr}`\n"
        f"Liq: ${liq:,.0f} | FDV: ${fdv:,.0f}\n"
        f"5m Vol: ${vol:,.0f}\n"
        f"[DexScreener]({get_dex_url(chain, pair_addr)})"
    )

# === RUG CHECK (BATCHED + CACHED) ===
async def is_safe_batch(addrs: list, chain: str, session: aiohttp.ClientSession) -> dict:
    if not addrs:
        return {}
    chain_id = 56 if chain == "BSC" else 1
    addrs_str = ",".join(addrs)
    url = GOPLUS_API.format(chain_id=chain_id, addrs=addrs_str)

    now = time.time()
    # Use cache if < 1h old
    cached = {a: goplus_cache[a] for a in addrs if a in goplus_cache and now - goplus_cache[a][1] < 3600}
    to_check = [a for a in addrs if a not in cached]

    results = cached.copy()
    if to_check:
        try:
            async with session.get(url, timeout=10) as r:
                if r.status == 200:
                    data = await r.json()
                    result = data.get("result", {})
                    for addr in to_check:
                        info = result.get(addr.lower(), {})
                        safe = (
                            info.get("is_open_source") == "1" and
                            info.get("honeypot") == "0" and
                            info.get("can_take_back_ownership") != "1"
                        )
                        results[addr] = safe
                        goplus_cache[addr] = (safe, now)
                else:
                    for addr in to_check:
                        results[addr] = False
        except Exception as e:
            logger.warning(f"GoPlus failed: {e}")
            for addr in to_check:
                results[addr] = False
    return results

# === COMMANDS ===
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    source = ctx.args[0] if ctx.args and ctx.args[0].startswith("track_") else "organic"
    influencer = source.split("_", 1)[1] if "_" in source else None

    if influencer and influencer not in tracker:
        tracker[influencer] = {"joins": 0, "subs": 0, "revenue": 0.0}
    if influencer:
        tracker[influencer]["joins"] += 1

    if uid not in users:
        users[uid] = {"free": FREE_ALERTS, "source": source, "paid": False}
    free = users[uid]["free"]

    await update.message.reply_text(
        f"ONION ALERTS\n\n"
        f"Free trial: `{free}` alerts left\n"
        f"Subscribe: `${PRICE}/mo`\n\n"
        f"**Pay USDT (BSC):**\n"
        f"`{WALLETS['BSC']}`\n\n"
        f"After payment, send TXID with:\n"
        f"`/pay YOUR_TXID_HERE`\n\n"
        f"_Auto-upgrade in <2 min!_",
        parse_mode="Markdown"
    )

    # Test alert
    test = (
        f"**TEST ALPHA SOL**\n"
        f"`ONIONCOIN`\n"
        f"**CA:** `onion123456789abcdefghi123456789abcdefghi`\n"
        f"Liq: $9,200 | FDV: $52,000\n"
        f"5m Vol: $15,600\n"
        f"[DexScreener](https://dexscreener.com/solana/onion123456789abcdefghi123456789abcdefghi)\n\n"
        f"_Test alert — real ones coming soon!_"
    )
    await ctx.bot.send_message(uid, test, parse_mode="Markdown", disable_web_page_preview=True)
    users[uid]["free"] -= 1
    save_data(data)

async def pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Payment processing is currently manual. Contact admin.")

async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/stats yourusername`", parse_mode="Markdown")
        return
    influencer = ctx.args[0].lower()
    if influencer not in tracker:
        await update.message.reply_text(f"No data for **{influencer}**.")
        return

    stats = tracker[influencer]
    revenue = stats["subs"] * PRICE
    your_cut = revenue * COMMISSION_RATE
    conv = stats["subs"] / max(stats["joins"], 1) * 100

    await update.message.reply_text(
        f"**{influencer.upper()} STATS**\n\n"
        f"Joins: `{stats['joins']}`\n"
        f"Paid Subs: `{stats['subs']}`\n"
        f"Revenue: **${revenue:.2f}**\n"
        f"**You Earn: ${your_cut:.2f}** (25%)\n"
        f"Conversion: `{conv:.1f}%`",
        parse_mode="Markdown"
    )

async def owner(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != YOUR_ADMIN_ID:
        return

    total_influencers = len(tracker)
    total_joins = sum(t["joins"] for t in tracker.values())
    total_subs = sum(t["subs"] for t in tracker.values())
    total_revenue = total_subs * PRICE
    owner_profit = total_revenue * (1 - COMMISSION_RATE)
    top = sorted(tracker.items(), key=lambda x: x[1]["subs"] * PRICE, reverse=True)[:10]
    top_list = "\n".join([f"{i+1}. {name} → ${stats['subs']*PRICE:.2f} ({stats['subs']} subs)" for i, (name, stats) in enumerate(top)])

    await update.message.reply_text(
        f"**OWNER DASHBOARD**\n\n"
        f"Influencers: `{total_influencers}`\n"
        f"Joins: `{total_joins}`\n"
        f"Subs: `{total_subs}`\n"
        f"Revenue: **${total_revenue:.2f}**\n"
        f"Your Profit: **${owner_profit:.2f}**\n\n"
        f"**TOP INFLUENCERS**\n{top_list or 'None'}",
        parse_mode="Markdown"
    )

# === SCANNER ===
async def scanner(app: Application):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        while True:
            try:
                now = time.time()
                candidates = []

                # === 1. New Pairs (SOL + BSC) ===
                for chain, slug in [("SOL", "solana"), ("BSC", "bsc")]:
                    try:
                        async with session.get(NEW_PAIRS_URL.format(chain=slug), timeout=10) as r:
                            if r.status == 200:
                                data = await r.json()
                                for p in data.get("pairs", [])[:50]:
                                    candidates.append((p, chain, slug, "new"))
                    except Exception as e:
                        logger.warning(f"New pairs {chain} failed: {e}")

                # === 2. Trending / Medium / High (search) ===
                for chain, slug in [("SOL", "solana"), ("BSC", "bsc")]:
                    try:
                        async with session.get(SEARCH_URL.format(chain=slug), timeout=10) as r:
                            if r.status == 200:
                                data = await r.json()
                                for p in data.get("pairs", [])[50:150]:  # avoid overlap
                                    candidates.append((p, chain, slug, "search"))
                    except Exception as e:
                        logger.warning(f"Search {chain} failed: {e}")

                if not candidates:
                    await asyncio.sleep(60)
                    continue

                # Extract addresses
                addr_to_pair = {}
                for p, chain, slug, src in candidates:
                    base = p.get("baseToken", {})
                    addr = base.get("address")
                    pair_addr = p.get("pairAddress")
                    if not addr or not pair_addr or addr in seen:
                        continue
                    addr_to_pair[addr] = (p, chain, slug, src, pair_addr)

                addrs = list(addr_to_pair.keys())
                if not addrs:
                    await asyncio.sleep(60)
                    continue

                # Batch rug check
                safety = await is_safe_batch(addrs, chain, session)
                safe_addrs = [a for a in addrs if safety.get(a, False)]

                # Process safe tokens
                alerts = []
                for addr in safe_addrs:
                    p, chain, slug, src, pair_addr = addr_to_pair[addr]
                    base = p.get("baseToken", {})
                    sym = base.get("symbol", "???")[:20]
                    liq = p.get("liquidity", {}).get("usd", 0) or 0
                    fdv = p.get("fdv", 0) or 0
                    vol = p.get("volume", {}).get("m5", 0) or 0

                    if addr in last_alerted and now - last_alerted[addr] < 300:
                        continue

                    # Volume spike
                    h = vol_hist[addr]
                    h.append(vol)
                    spike = vol / (sum(h) / len(h)) if len(h) > 1 else 1.0
                    volume_spike = spike >= 1.5

                    # Whale buy (from pair detail or txns in payload)
                    whale_buy = False
                    txns = p.get("txns", {}).get("m5", {})
                    buys = txns.get("buys", [])
                    if isinstance(buys, list):
                        whale_buy = any(b.get("total", 0) >= 2000 for b in buys[:5])

                    # === Tier Logic ===
                    reason = []

                    # New Pairs Tier
                    if src == "new" and liq >= 2000 and fdv >= 20000 and vol >= 1000:
                        reason.append("New Pair")

                    # Medium Tier
                    if liq >= 20000 and fdv >= 70000 and vol >= 3000:
                        reason.append("Medium")

                    # High Tier
                    if volume_spike:
                        reason.append(f"Spike {spike:.1f}x")
                    if whale_buy:
                        reason.append("Whale ≥$2K")

                    if reason:
                        seen.add(addr)
                        last_alerted[addr] = now
                        reason_str = " | ".join(reason)
                        msg = format_alert(chain, sym, addr, liq, fdv, vol, pair_addr, reason_str)
                        alerts.append((msg, addr))

                # === Send Alerts ===
                sent_total = 0
                for msg, addr in alerts:
                    sent = 0
                    for uid, u in list(users.items()):
                        if u["free"] > 0 or u.get("paid", False):
                            try:
                                await app.bot.send_message(uid, msg, parse_mode="Markdown", disable_web_page_preview=True)
                                if u["free"] > 0:
                                    u["free"] -= 1
                                sent += 1
                                sent_total += 1
                                if sent % 10 == 0:
                                    await asyncio.sleep(0.5)
                            except:
                                pass
                    logger.info(f"ALERT → {sym} | {reason_str} | {sent} users")

                # Prune old vol_hist
                for addr in list(vol_hist.keys()):
                    if addr not in addr_to_pair:
                        del vol_hist[addr]

                await asyncio.sleep(60)

            except Exception as e:
                logger.error(f"SCANNER CRASH: {e}")
                await asyncio.sleep(60)

# === MAIN ===
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("owner", owner))

    # Background tasks
    loop = asyncio.get_event_loop()
    loop.create_task(scanner(app))
    loop.create_task(auto_save())

    logger.info("ONION ALERTS LIVE — SENDING CA FROM DEXSCREENER")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        save_data(data)
        logger.info("Shutdown complete.")
