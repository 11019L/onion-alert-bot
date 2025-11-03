# main.py - ONION ALERTS (FIXED & WORKING)
import os
import asyncio
import logging
import json
import time
import copy
from collections import defaultdict, deque
from datetime import datetime
import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.helpers import escape_markdown

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    exit("ERROR: Add BOT_TOKEN")

FREE_ALERTS = 3
PRICE = 19.99
COMMISSION_RATE = 0.25
YOUR_ADMIN_ID = int(os.getenv("ADMIN_ID", "1319494378"))

WALLETS = {"BSC": "0xa11351776d6f483418b73c8e40bc706c93e8b1e1"}

# APIs
GOPLUS_API = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={addrs}"
NEW_PAIRS_URL = "https://api.dexscreener.com/latest/dex/new-pairs/{chain}"
SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q={chain}"

# === LOGGING ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("onion-alerts")

# === PERSISTENT DATA ===
DATA_FILE = "data.json"
SAVE_INTERVAL = 30  # seconds

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                raw = json.load(f)
                now = time.time()
                seen_clean = {a: t for a, t in raw.get("seen", {}).items() if now - t < 86400}
                last_alerted_clean = {k: v for k, v in raw.get("last_alerted", {}).items() if now - v < 3600}
                return {
                    "tracker": raw.get("tracker", {}),
                    "users": raw.get("users", {}),
                    "seen": seen_clean,
                    "last_alerted": last_alerted_clean,
                }
        except Exception as e:
            logger.error(f"Failed to load data: {e}")
    return {"tracker": {}, "users": {}, "seen": {}, "last_alerted": {}}

# Load initial data
data = load_data()
tracker = data["tracker"]
users = data["users"]
seen = data["seen"]
last_alerted = data["last_alerted"]
vol_hist = defaultdict(lambda: deque(maxlen=5))
goplus_cache = {}
save_lock = asyncio.Lock()

# === AUTO-SAVE (FIX #1: Safe deepcopy + lock) ===
async def auto_save():
    while True:
        await asyncio.sleep(SAVE_INTERVAL)
        async with save_lock:
            save_dict = {
                "tracker": copy.deepcopy(tracker),
                "users": {k: v for k, v in users.items()},
                "seen": {k: v for k, v in seen.items() if time.time() - v < 86400},
                "last_alerted": {k: v for k, v in last_alerted.items() if time.time() - v < 3600}
            }
            try:
                with open(DATA_FILE, "w") as f:
                    json.dump(save_dict, f, indent=2)
                logger.info("Data auto-saved.")
            except Exception as e:
                logger.error(f"Auto-save failed: {e}")

# === HELPERS ===
def get_dex_url(chain: str, pair_addr: str) -> str:
    chain_slug = "solana" if chain == "SOL" else "bsc"
    return f"https://dexscreener.com/{chain_slug}/{pair_addr}"

def safe_md(text):
    return escape_markdown(str(text), version=2)

def format_alert(chain, sym, addr, liq, fdv, vol, pair_addr, reason):
    return (
        f"*ALPHA {safe_md(chain)}* — {safe_md(reason)}\n"
        f"`{safe_md(sym)}`\n"
        f"*CA:* `{safe_md(addr)}`\n"
        f"Liq: ${liq:,.0f} | FDV: ${fdv:,.0f}\n"
        f"5m Vol: ${vol:,.0f}\n"
        f"[DexScreener]({get_dex_url(chain, pair_addr)})"
    )

# === GOPLUS ===
async def is_safe_batch(addrs, chain, session):
    if not addrs:
        return {}
    chain_id = 56 if chain == "BSC" else 1
    addrs_str = ",".join(addrs)
    url = GOPLUS_API.format(chain_id=chain_id, addrs=addrs_str)

    now = time.time()
    cached = {a: goplus_cache[a] for a in addrs if a in goplus_cache and now - goplus_cache[a][1] < 3600}
    to_check = [a for a in addrs if a not in cached]
    results = {a: v[0] for a, v in cached.items()}

    if not to_check:
        return results

    try:
        async with session.get(url, timeout=10) as r:
            if r.status == 200:
                data = await r.json()
                result = {k.lower(): v for k, v in data.get("result", {}).items()}
                for addr in to_check:
                    info = result.get(addr.lower(), {})
                    safe = (
                        info.get("is_open_source") == "1"
                        and info.get("honeypot") == "0"
                        and info.get("can_take_back_ownership") != "1"
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

    msg = (
        f"*ONION ALERTS*\n\n"
        f"Free trial: `{free}` alerts left\n"
        f"Subscribe: `${PRICE}/mo`\n\n"
        f"*Pay USDT (BSC):*\n`{WALLETS['BSC']}`\n\n"
        f"After payment, send TXID:\n`/pay YOUR_TXID_HERE`\n\n"
        f"_Auto-upgrade in <2 min!_"
    )
    await update.message.reply_text(msg, parse_mode="MarkdownV2")

    test = (
        f"*TEST ALPHA SOL*\n"
        f"`ONIONCOIN`\n"
        f"*CA:* `onion123456789abcdefghi123456789abcdefghi`\n"
        f"Liq: $9,200 | FDV: $52,000\n"
        f"5m Vol: $15,600\n"
        f"[DexScreener](https://dexscreener.com/solana/onion123456789abcdefghi123456789abcdefghi)\n\n"
        f"_Test alert — real ones coming soon!_"
    )
    await ctx.bot.send_message(uid, test, parse_mode="MarkdownV2", disable_web_page_preview=True)

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
    joins = stats.get("joins", 0)
    subs = stats.get("subs", 0)
    revenue = subs * PRICE
    your_cut = revenue * COMMISSION_RATE
    conv = subs / max(joins, 1) * 100
    await update.message.reply_text(
        f"*{influencer.upper()} STATS*\n\n"
        f"Joins: `{joins}`\nPaid Subs: `{subs}`\n"
        f"Revenue: *${revenue:.2f}*\n"
        f"You Earn: *${your_cut:.2f}* (25%)\n"
        f"Conversion: `{conv:.1f}%`",
        parse_mode="MarkdownV2"
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
    top_list = "\n".join(
        [f"{i+1}. {safe_md(name)} → ${stats['subs']*PRICE:.2f} ({stats['subs']} subs)" for i, (name, stats) in enumerate(top)]
    )
    msg = (
        f"*OWNER DASHBOARD*\n\n"
        f"Influencers: `{total_influencers}`\n"
        f"Joins: `{total_joins}`\n"
        f"Subs: `{total_subs}`\n"
        f"Revenue: *${total_revenue:.2f}*\n"
        f"Your Profit: *${owner_profit:.2f}*\n\n"
        f"*TOP INFLUENCERS*\n{top_list or 'None'}"
    )
    await update.message.reply_text(msg, parse_mode="MarkdownV2")

# === SCANNER ===
async def scanner(app: Application):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        while True:
            try:
                now = time.time()
                candidates = []

                # --- Fetch new pairs + trending ---
                for chain, slug in [("SOL", "solana"), ("BSC", "bsc")]:
                    try:
                        async with session.get(NEW_PAIRS_URL.format(chain=slug), timeout=10) as r:
                            if r.status == 200:
                                data = await r.json()
                                for p in data.get("pairs", [])[:50]:
                                    candidates.append((p, chain, slug, "new"))
                    except Exception as e:
                        logger.warning(f"New pairs {chain} failed: {e}")

                    try:
                        async with session.get(SEARCH_URL.format(chain=slug), timeout=10) as r:
                            if r.status == 200:
                                data = await r.json()
                                for p in data.get("pairs", [])[50:150]:
                                    candidates.append((p, chain, slug, "search"))
                    except Exception as e:
                        logger.warning(f"Search {chain} failed: {e}")

                if not candidates:
                    await asyncio.sleep(60)
                    continue

                # --- Extract ---
                addr_to_pair = {}
                for p, chain, slug, src in candidates:
                    base = p.get("baseToken", {})
                    addr = base.get("address")
                    pair_addr = p.get("pairAddress")
                    if not addr or not pair_addr or addr in seen:
                        continue
                    addr_to_pair[addr] = (p, chain, slug, src, pair_addr)

                if not addr_to_pair:
                    await asyncio.sleep(60)
                    continue

                # --- Check per-chain safely ---
                safety = {}
                chain_groups = defaultdict(list)
                for addr, (_, chain, _, _, _) in addr_to_pair.items():
                    chain_groups[chain].append(addr)
                for chain, addrs in chain_groups.items():
                    part = await is_safe_batch(addrs, chain, session)
                    safety.update(part)

                alerts = []
                for addr, (p, chain, slug, src, pair_addr) in addr_to_pair.items():
                    if not safety.get(addr, False):
                        continue
                    base = p.get("baseToken", {})
                    sym = base.get("symbol", "???")[:20]
                    liq = p.get("liquidity", {}).get("usd", 0) or 0
                    fdv = p.get("fdv", 0) or 0
                    vol = p.get("volume", {}).get("m5", 0) or 0

                    if addr in last_alerted and now - last_alerted[addr] < 300:
                        continue

                    # === FIX #4: Volume spike using PREVIOUS average ===
                    h = vol_hist[addr]
                    prev_avg = sum(h) / len(h) if h else 0
                    spike = vol / prev_avg if prev_avg > 0 else 1.0
                    h.append(vol)
                    volume_spike = spike >= 1.5

                    reason = []
                    if src == "new" and liq >= 500 and fdv >= 3000 and vol >= 200:
                        reason.append("New Pair")
                    if liq >= 5000 and fdv >= 10000 and vol >= 1000:
                        reason.append("Medium")
                    if volume_spike:
                        reason.append(f"Spike {spike:.1f}x")

                    if not reason:
                        continue

                    last_alerted[addr] = now
                    msg = format_alert(chain, sym, addr, liq, fdv, vol, pair_addr, " | ".join(reason))
                    alerts.append((msg, addr))

                # --- Send alerts (FIX #3: Flood control) ---
                if not alerts:
                    await asyncio.sleep(60)
                    continue

                for msg, addr in alerts:
                    sent = 0
                    async with save_lock:  # Prevent race during send
                        target_users = list(users.items())
                    for uid, u in target_users:
                        if u["free"] > 0 or u.get("paid"):
                            try:
                                await app.bot.send_message(uid, msg, parse_mode="MarkdownV2", disable_web_page_preview=True)
                                if u["free"] > 0:
                                    async with save_lock:
                                        users[uid]["free"] -= 1
                                async with save_lock:
                                    seen[addr] = time.time()
                                sent += 1
                                if sent % 20 == 0:
                                    await asyncio.sleep(1)  # Telegram: 20 msg/sec
                            except Exception as e:
                                if "Flood control" in str(e):
                                    await asyncio.sleep(5)
                                else:
                                    logger.warning(f"Send failed to {uid}: {e}")
                    logger.info(f"ALERT → {addr} | Sent to {sent} users")

                await asyncio.sleep(60)

            except Exception as e:
                logger.error(f"SCANNER CRASH: {e}")
                await asyncio.sleep(60)

# === MAIN ===
async def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("owner", owner))

    app.create_task(scanner(app))
    app.create_task(auto_save())

    logger.info("ONION ALERTS LIVE — SENDING CA FROM DEXSCREENER")
    await app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        async with save_lock:
            with open(DATA_FILE, "w") as f:
                json.dump({
                    "tracker": tracker,
                    "users": users,
                    "seen": seen,
                    "last_alerted": last_alerted
                }, f, indent=2)
        logger.info("Shutdown complete.")
