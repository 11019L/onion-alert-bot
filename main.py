# main.py - ONION ALERTS: FULLY AUTOMATIC + NEW + MEDIUM + HIGH PAIRS
import os, asyncio, logging, json, time, re
from collections import defaultdict, deque
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import aiohttp
from telegram.constants import ParseMode

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

# GoPlus API
GOPLUS_API = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={addrs}"

# DexScreener
NEW_PAIRS_URL = "https://api.dexscreener.com/latest/dex/new-pairs/{chain}"
SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q={chain}"
PAIR_DETAIL_URL = "https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair_addr}"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === PERSISTENT DATA ===
DATA_FILE = "data.json"
SAVE_INTERVAL = 30  # seconds

def load_data():
    now = time.time()
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                raw = json.load(f)
                # Clean seen: keep last 24h
                seen = [(a, t) for a, t in raw.get("seen", []) if now - t < 86400]
                last_alerted = {k: v for k, v in raw.get("last_alerted", {}).items() if now - v < 3600}
                vol_hist_data = raw.get("vol_hist", {})
                vol_hist = defaultdict(lambda: deque(maxlen=5))
                for addr, vols in vol_hist_data.items():
                    vol_hist[addr.lower()].extend([v for v in vols[-5:] if v > 0])
                return {
                    "tracker": raw.get("tracker", {}),
                    "users": raw.get("users", {}),
                    "seen": seen,
                    "last_alerted": last_alerted,
                    "vol_hist": vol_hist,
                    "pending_payments": raw.get("pending_payments", {}),  # txid -> uid
                }
        except Exception as e:
            logger.error(f"Failed to load data: {e}")
    # Defaults
    return {
        "tracker": {},
        "users": {},
        "seen": [],
        "last_alerted": {},
        "vol_hist": defaultdict(lambda: deque(maxlen=5)),
        "pending_payments": {},
    }

def save_data(state):
    now = time.time()
    clean_seen = [(a, t) for a, t in state["seen"] if now - t < 86400]
    with open(DATA_FILE, "w") as f:
        json.dump({
            "tracker": state["tracker"],
            "users": state["users"],
            "seen": clean_seen,
            "last_alerted": state["last_alerted"],
            "vol_hist": {k: list(v) for k, v in state["vol_hist"].items() if v},
            "pending_payments": state["pending_payments"],
        }, f, indent=2)

# Load state
state = load_data()
tracker = state["tracker"]
users = state["users"]
seen = state["seen"]  # list of (addr, ts)
seen_set = {a.lower() for a, _ in seen}
last_alerted = state["last_alerted"]
vol_hist = state["vol_hist"]
pending_payments = state["pending_payments"]

# === AUTO-SAVE ===
async def auto_save():
    while True:
        await asyncio.sleep(SAVE_INTERVAL)
        save_data(state)

# === HELPERS ===
def get_dex_url(chain: str, pair_addr: str) -> str:
    chain_slug = "solana" if chain == "SOL" else "bsc"
    return f"https://dexscreener.com/{chain_slug}/{pair_addr}"

def escape_md(text: str) -> str:
    return re.sub(r'([_*[\]()~`>#+\-=|{}.!])', r'\\\1', text)

def format_alert(chain: str, sym: str, addr: str, liq: float, fdv: float, vol: float, pair_addr: str, reason: str):
    sym_safe = escape_md(sym[:20])
    addr_safe = escape_md(addr)
    return (
        f"*ALPHA {chain}* [{reason}]\n"
        f"`{sym_safe}`\n"
        f"*CA:* `{addr_safe}`\n"
        f"Liq: ${liq:,.0f} \\| FDV: ${fdv:,.0f}\n"
        f"5m Vol: ${vol:,.0f}\n"
        f"[DexScreener]({get_dex_url(chain, pair_addr)})"
    )

# === RUG CHECK (BATCHED + CACHED) ===
goplus_cache = {}  # addr_lower -> (is_safe, timestamp)

async def is_safe_batch(addrs: list, chain: str, session: aiohttp.ClientSession) -> dict:
    if not addrs:
        return {}
    chain_id = 56 if chain == "BSC" else 1
    addrs_str = ",".join(addrs)
    url = GOPLUS_API.format(chain_id=chain_id, addrs=addrs_str)

    now = time.time()
    addrs_lower = [a.lower() for a in addrs]
    cached = {a: goplus_cache[a] for a in addrs_lower if a in goplus_cache and now - goplus_cache[a][1] < 3600}
    to_check = [a for a in addrs_lower if a not in cached]

    results = {a.upper(): cached[a][0] for a in cached}  # back to original case

    if to_check:
        try:
            async with session.get(url, timeout=10) as r:
                if r.status == 200:
                    data = await r.json()
                    result = data.get("result", {})
                    for addr_lower in to_check:
                        info = result.get(addr_lower, {})
                        safe = (
                            info.get("is_open_source") == "1" and
                            info.get("honeypot") == "0" and
                            info.get("can_take_back_ownership") != "1"
                        )
                        results[addr_lower.upper()] = safe
                        goplus_cache[addr_lower] = (safe, now)
                else:
                    for a in to_check:
                        results[a.upper()] = False
                        goplus_cache[a] = (False, now)
        except Exception as e:
            logger.warning(f"GoPlus failed: {e}")
            for a in to_check:
                results[a.upper()] = False
                goplus_cache[a] = (False, now)
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
        f"*Pay USDT \\(BSC\\):*\n"
        f"`{WALLETS['BSC']}`\n\n"
        f"After payment, send TXID with:\n"
        f"`/pay YOUR_TXID_HERE`\n\n"
        f"_Auto\\-upgrade in \\<2 min\\!_"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True)

    # Test alert only if free > 0
    if users[uid]["free"] > 0:
        test = (
            f"*TEST ALPHA SOL*\n"
            f"`ONIONCOIN`\n"
            f"*CA:* `onion123456789abcdefghi123456789abcdefghi`\n"
            f"Liq: $9,200 \\| FDV: $52,000\n"
            f"5m Vol: $15,600\n"
            f"[DexScreener](https://dexscreener.com/solana/onion123456789abcdefghi123456789abcdefghi)\n\n"
            f"_Test alert — real ones coming soon\\!_"
        )
        await ctx.bot.send_message(uid, test, parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True)
        users[uid]["free"] -= 1
        save_data(state)

async def pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if len(ctx.args) != 1:
        await update.message.reply_text("Usage: `/pay YOUR_TXID`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    txid = ctx.args[0].strip()
    if not re.match(r'^0x[a-fA-F0-9]{64}$', txid):
        await update.message.reply_text("Invalid TXID format\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    if txid in pending_payments:
        await update.message.reply_text("This TXID is already pending\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    pending_payments[txid] = uid
    save_data(state)

    await ctx.bot.send_message(
        YOUR_ADMIN_ID,
        f"*New Payment*\nUser: `{uid}`\nTX: `{txid}`\nApprove: `/approve {uid}`",
        parse_mode=ParseMode.MARKDOWN_V2
    )
    await update.message.reply_text("TXID received\\. Admin will verify in \\<5 min\\.", parse_mode=ParseMode.MARKDOWN_V2)

async def approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != YOUR_ADMIN_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Usage: `/approve USER_ID`", parse_mode=ParseMode.MARKDOWN_V2)
        return
    try:
        uid = int(ctx.args[0])
        if uid not in users:
            await update.message.reply_text("User not found\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return
        users[uid]["paid"] = True
        users[uid]["free"] = 0
        save_data(state)
        await ctx.bot.send_message(uid, "*Subscription activated\\! Unlimited alerts\\.*", parse_mode=ParseMode.MARKDOWN_V2)
        await update.message.reply_text(f"User {uid} upgraded\\.", parse_mode=ParseMode.MARKDOWN_V2)
    except:
        await update.message.reply_text("Invalid user ID\\.", parse_mode=ParseMode.MARKDOWN_V2)

async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/stats yourusername`", parse_mode=ParseMode.MARKDOWN_V2)
        return
    influencer = ctx.args[0].lower()
    if influencer not in tracker:
        await update.message.reply_text(f"No data for *{escape_md(influencer)}*\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    stats = tracker[influencer]
    revenue = stats["subs"] * PRICE
    your_cut = revenue * COMMISSION_RATE
    conv = stats["subs"] / max(stats["joins"], 1) * 100

    await update.message.reply_text(
        f"*{influencer.upper()} STATS*\n\n"
        f"Joins: `{stats['joins']}`\n"
        f"Paid Subs: `{stats['subs']}`\n"
        f"Revenue: *${revenue:.2f}*\n"
        f"*You Earn: ${your_cut:.2f}* \\(25\\%\\)\n"
        f"Conversion: `{conv:.1f}%`",
        parse_mode=ParseMode.MARKDOWN_V2
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
    top_list = "\n".join([f"{i+1}\\. {escape_md(name)} → ${stats['subs']*PRICE:.2f} \\({stats['subs']} subs\\)" for i, (name, stats) in enumerate(top)])

    await update.message.reply_text(
        f"*OWNER DASHBOARD*\n\n"
        f"Influencers: `{total_influencers}`\n"
        f"Joins: `{total_joins}`\n"
        f"Subs: `{total_subs}`\n"
        f"Revenue: *${total_revenue:.2f}*\n"
        f"Your Profit: *${owner_profit:.2f}*\n\n"
        f"*TOP INFLUENCERS*\n{top_list or 'None'}",
        parse_mode=ParseMode.MARKDOWN_V2
    )

# === SCANNER ===
async def scanner(app: Application):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        while True:
            try:
                now = time.time()
                candidates = []

                # === 1. New Pairs ===
                for chain, slug in [("SOL", "solana"), ("BSC", "bsc")]:
                    try:
                        async with session.get(NEW_PAIRS_URL.format(chain=slug), timeout=10) as r:
                            if r.status == 200:
                                data = await r.json()
                                for p in data.get("pairs", [])[:50]:
                                    candidates.append((p, chain, slug, "new"))
                    except Exception as e:
                        logger.warning(f"New pairs {chain} failed: {e}")

                # === 2. Search (Medium/High) ===
                for chain, slug in [("SOL", "solana"), ("BSC", "bsc")]:
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

                # Extract addresses
                addr_to_pair = {}
                for p, chain, slug, src in candidates:
                    base = p.get("baseToken", {})
                    addr = base.get("address")
                    pair_addr = p.get("pairAddress")
                    if not addr or not pair_addr:
                        continue
                    addr_lower = addr.lower()
                    if addr_lower in seen_set:
                        continue
                    addr_to_pair[addr_lower] = (p, chain, slug, src, pair_addr, addr)  # keep original case

                addrs_lower = list(addr_to_pair.keys())
                if not addrs_lower:
                    await asyncio.sleep(60)
                    continue

                # Batch rug check per chain
                safety = {}
                for chain in ["SOL", "BSC"]:
                    addrs_in_chain = [a for a in addrs_lower if addr_to_pair[a][1] == chain]
                    if addrs_in_chain:
                        chain_safety = await is_safe_batch(
                            [addr_to_pair[a][5] for a in addrs_in_chain],  # original case
                            chain, session
                        )
                        safety.update(chain_safety)

                safe_addrs = [a for a in addrs_lower if safety.get(addr_to_pair[a][5], False)]

                # Process alerts
                alerts = []
                for addr_lower in safe_addrs:
                    p, chain, slug, src, pair_addr, addr_orig = addr_to_pair[addr_lower]
                    base = p.get("baseToken", {})
                    sym = base.get("symbol", "???")
                    liq = p.get("liquidity", {}).get("usd", 0) or 0
                    fdv = p.get("fdv", 0) or 0
                    vol = p.get("volume", {}).get("m5", 0) or 0

                    if addr_lower in last_alerted and now - last_alerted[addr_lower] < 300:
                        continue

                    # Volume spike
                    h = vol_hist[addr_lower]
                    prev_avg = sum(h) / len(h) if len(h) > 0 else 0
                    h.append(vol)
                    spike = vol / (prev_avg or 1) if prev_avg > 0 else 1.0
                    volume_spike = spike >= 1.5

                    # Whale buy
                    whale_buy = False
                    txns = p.get("txns", {}).get("m5", {})
                    buys = txns.get("buys", [])
                    if isinstance(buys, list):
                        whale_buy = any(b.get("total", 0) >= 2000 for b in buys[:5])

                    # Tier logic
                    reason = []
                    if src == "new" and liq >= 2000 and fdv >= 20000 and vol >= 1000:
                        reason.append("New Pair")
                    if liq >= 20000 and fdv >= 70000 and vol >= 3000:
                        reason.append("Medium")
                    if volume_spike:
                        reason.append(f"Spike {spike:.1f}x")
                    if whale_buy:
                        reason.append("Whale ≥$2K")

                    if reason:
                        seen.append((addr_lower, now))
                        seen_set.add(addr_lower)
                        last_alerted[addr_lower] = now
                        reason_str = " \\| ".join(reason)
                        msg = format_alert(chain, sym, addr_orig, liq, fdv, vol, pair_addr, reason_str)
                        alerts.append((msg, addr_lower, sym, reason_str))

                # Send alerts with rate limiting
                sent_total = 0
                for msg, addr_lower, sym, reason_str in alerts:
                    sent = 0
                    for uid, u in list(users.items()):
                        if u["free"] > 0 or u.get("paid", False):
                            try:
                                await app.bot.send_message(
                                    uid, msg,
                                    parse_mode=ParseMode.MARKDOWN_V2,
                                    disable_web_page_preview=True
                                )
                                if u["free"] > 0:
                                    u["free"] -= 1
                                sent += 1
                                sent_total += 1
                                if sent % 20 == 0:
                                    await asyncio.sleep(1)
                            except Exception as e:
                                if "Flood" in str(e):
                                    await asyncio.sleep(5)
                                logger.warning(f"Send failed to {uid}:
