# main.py - ONION ALERTS: FULLY AUTOMATIC + OLD + NEW PAIRS
import os, asyncio, logging, json, time
from collections import defaultdict, deque
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import aiohttp

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    exit("ERROR: Add BOT_TOKEN")

FREE_ALERTS = 3
PRICE = 19.99
COMMISSION_RATE = 0.25
YOUR_ADMIN_ID = 1319494378

WALLETS = {
    "BSC": "0xa11351776d6f483418b73c8e40bc706c93e8b1e1"
}

# GoPlus API (free tier)
GOPLUS_API = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={addr}"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === PERSISTENT DATA ===
DATA_FILE = "data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"tracker": {}, "users": {}, "seen": [], "last_alerted": {}}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        # Clean seen & last_alerted
        data["seen"] = [s for s in data["seen"] if time.time() - s[1] < 86400]  # 24h
        data["last_alerted"] = {k: v for k, v in data["last_alerted"].items() if time.time() - v < 3600}
        json.dump(data, f)

data = load_data()
tracker = data.get("tracker", {})
users = data.get("users", {})
seen = set(addr for addr, ts in data.get("seen", []))
last_alerted = data.get("last_alerted", {})
vol_hist = defaultdict(lambda: deque(maxlen=5))
pending_payments = {}  # txid -> uid

# === HELPERS ===
def save_all():
    save_data({"tracker": tracker, "users": users, "seen": [(a, time.time()) for a in seen], "last_alerted": last_alerted})

async def broadcast(app, msg):
    sent = 0
    for uid in list(users.keys()):
        try:
            await app.bot.send_message(uid, msg, parse_mode="Markdown", disable_web_page_preview=True)
            sent += 1
            if sent % 10 == 0:
                await asyncio.sleep(1)  # Avoid flood
        except Exception as e:
            logger.warning(f"Failed to send to {uid}: {e}")
    return sent

# === COMMANDS ===
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    source = ctx.args[0] if ctx.args and ctx.args[0].startswith("track_") else "organic"
    influencer = source.split("_", 1)[1] if source.startswith("track_") else None

    if influencer and influencer not in tracker:
        tracker[influencer] = {"joins": 0, "subs": 0, "revenue": 0.0}
    if influencer:
        tracker[influencer]["joins"] += 1

    if uid not in users:
        users[uid] = {"free": FREE_ALERTS, "source": source, "paid": False}
    free = users[uid]["free"]

    await update.message.reply_text(
        f"🚀 **ONION ALERTS** 🚀\n\n"
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
    save_all()

async def pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if len(ctx.args) != 1:
        await update.message.reply_text("Usage: `/pay YOUR_TXID`", parse_mode="Markdown")
        return
    txid = ctx.args[0].strip()
    pending_payments[txid.lower()] = uid
    await update.message.reply_text(f"✅ TXID `{txid}` recorded. Checking payment...", parse_mode="Markdown")
    save_all()

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
async def is_safe(addr: str, chain: str, session: aiohttp.ClientSession) -> bool:
    chain_id = 56 if chain == "BSC" else 1
    url = GOPLUS_API.format(chain_id=chain_id, addr=addr)
    try:
        async with session.get(url, timeout=10) as r:
            if r.status != 200: return False
            data = await r.json()
            result = data.get("result", {}).get(addr.lower(), {})
            return (
                result.get("is_open_source") == "1" and
                result.get("honeypot") == "0" and
                result.get("can_take_back_ownership") != "1"
            )
    except:
        return False

async def scanner(app: Application):
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                now = time.time()
                for chain, url in [("SOL", "solana"), ("BSC", "bsc")]:
                    # 1. New Pairs
                    async with s.get(f"https://api.dexscreener.com/latest/dex/new-pairs/{url}", timeout=15) as nr:
                        if nr.status == 200:
                            data = await nr.json()
                            for p in data.get("pairs", [])[:50]:
                                await process_pair(p, chain, url, s, app, now)

                    # 2. Trending Pairs
                    async with s.get(f"https://api.dexscreener.com/latest/dex/search?q={url}", timeout=15) as tr:
                        if tr.status == 200:
                            data = await tr.json()
                            for p in data.get("pairs", [])[50:100]:
                                await process_pair(p, chain, url, s, app, now)

                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"SCANNER ERROR: {e}")
                await asyncio.sleep(60)

async def process_pair(p, chain, url, s, app, now):
    base = p.get("baseToken", {})
    addr = base.get("address")
    pair_addr = p.get("pairAddress")
    if not addr or not pair_addr or addr in seen:
        return

    liq = p.get("liquidity", {}).get("usd", 0)
    fdv = p.get("fdv", 0)
    vol = p.get("volume", {}).get("m5", 0)
    sym = base.get("symbol", "??")[:20]

    # Rug Check
    if not await is_safe(addr, chain, s):
        return

    # Volume Spike
    h = vol_hist[addr]; h.append(vol)
    spike = vol / (sum(h)/len(h)) if len(h) > 1 else 1
    volume_spike = spike >= 1.5

    # Whale Buy
    whale_buy = False
    try:
        async with s.get(f"https://api.dexscreener.com/latest/dex/pairs/{url}/{pair_addr}", timeout=10) as tr:
            if tr.status == 200:
                data = await tr.json()
                buys = data.get("pair", {}).get("txns", {}).get("m5", {}).get("buys", [])
                whale_buy = any(b.get("total", 0) >= 2000 for b in buys[:5])
    except:
        pass

    # Cooldown
    if addr in last_alerted and now - last_alerted[addr] < 300:  # 5 min
        return

    # Triggers
    trigger_base = liq >= 30000 and fdv >= 70000 and vol >= 3000
    trigger_spike = volume_spike
    trigger_whale = whale_buy

    if trigger_base or trigger_spike or trigger_whale:
        reason = []
        if trigger_base: reason.append("High Liq+FDV+Vol")
        if trigger_spike: reason.append(f"Spike {spike:.1f}x")
        if trigger_whale: reason.append("Whale ≥$2K")
        reason_str = " | ".join(reason)

        seen.add(addr)
        last_alerted[addr] = now

        msg = (
            f"**ALPHA {chain}** [{reason_str}]\n"
            f"`{sym}`\n"
            f"**CA:** `{addr}`\n"
            f"Liq: ${liq:,.0f} | FDV: ${fdv:,.0f}\n"
            f"5m Vol: ${vol:,.0f}\n"
            f"[DexScreener](https://dexscreener.com/{url}/{pair_addr})"
        )

        sent = 0
        for uid, d in list(users.items()):
            if d["free"] > 0 or d.get("paid", False):
                try:
                    await app.bot.send_message(uid, msg, parse_mode="Markdown", disable_web_page_preview=True)
                    if d["free"] > 0:
                        d["free"] -= 1
                    sent += 1
                    if sent % 10 == 0:
                        await asyncio.sleep(1)
                except:
                    pass
        logger.info(f"ALERT → {sym} | {reason_str} | {sent} users")

# === PAYMENT CHECKER ===
async def check_payments(app: Application):
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                url = f"https://api.bscscan.com/api"
                params = {
                    "module": "account",
                    "action": "tokentx",
                    "contractaddress": "0x55d398326f99059fF775485246999027B3197955",  # USDT
                    "address": WALLETS["BSC"],
                    "sort": "desc",
                    "apikey": ""  # Optional
                }
                async with s.get(url, params=params, timeout=15) as r:
                    if r.status != 200: continue
                    data = await r.json()
                    for tx in data.get("result", [])[:20]:
                        txid = tx["hash"].lower()
                        if txid in pending_payments:
                            value = int(tx["value"]) / 1e18
                            if 19.9 <= value <= 20.1:
                                uid = pending_payments.pop(txid)
                                if uid in users and not users[uid]["paid"]:
                                    users[uid]["paid"] = True
                                    source = users[uid]["source"]
                                    influencer = source.split("_", 1)[1] if source.startswith("track_") else None
                                    if influencer and influencer in tracker:
                                        tracker[influencer]["subs"] += 1
                                        tracker[influencer]["revenue"] += PRICE
                                    await app.bot.send_message(uid, "✅ **Payment confirmed! Unlimited alerts ON.**", parse_mode="Markdown")
                                    logger.info(f"PAID: {uid} via {txid}")
                    save_all()
                await asyncio.sleep(45)
            except Exception as e:
                logger.error(f"PAYMENT ERROR: {e}")
                await asyncio.sleep(45)

# === MAIN ===
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("owner", owner))

    loop = asyncio.get_event_loop()
    loop.create_task(scanner(app))
    loop.create_task(check_payments(app))

    logger.info("ONION ALERTS LIVE — OLD + NEW PAIRS")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        save_all()
        logger.info("Shutdown complete.")
