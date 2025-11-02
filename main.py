# main.py - ONION ALERTS: FULLY AUTOMATIC + OLD + NEW PAIRS + RUG CHECK
import os, asyncio, logging, json, time
from collections import defaultdict, deque
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

WALLETS = {"BSC": "0xa11351776d6f483418b73c8e40bc706c93e8b1e1"}
USDT_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"

# GoPlus Labs API (free)
GOPLUS_API = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={addr}"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === DATA ===
DATA_FILE = "data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"tracker": {}, "users": {}, "seen": [], "last_alerted": {}}

def save_data(data):
    data["seen"] = [s for s in data["seen"] if time.time() - s[1] < 86400]
    data["last_alerted"] = {k: v for k, v in data["last_alerted"].items() if time.time() - v < 3600}
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

data = load_data()
tracker = data.get("tracker", {})
users = data.get("users", {})
seen = set(addr for addr, ts in data.get("seen", []))
last_alerted = data.get("last_alerted", {})
vol_hist = defaultdict(lambda: deque(maxlen=5))
pending_payments = {}

def save_all():
    save_data({"tracker": tracker, "users": users, "seen": [(a, time.time()) for a in seen], "last_alerted": last_alerted})

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
        f"**ONION ALERTS**\n\n"
        f"Free: `{free}` alerts\n"
        f"Subscribe: `${PRICE}/mo`\n\n"
        f"**Pay USDT (BSC):**\n`{WALLETS['BSC']}`\n\n"
        f"After send: `/pay TXID`\n"
        f"_Auto-upgrade in <2 min!_",
        parse_mode="Markdown"
    )

    # Test alert
    test = (
        f"**TEST ALPHA**\n"
        f"`ONION`\n"
        f"**CA:** `onion123456789abcdefghi123456789abcdefghi`\n"
        f"Liq: $9,200 | FDV: $52,000\n"
        f"5m Vol: $15,600\n"
        f"[DexScreener](https://dexscreener.com/solana/onion123456789abcdefghi123456789abcdefghi)\n"
        f"_Test alert_"
    )
    await ctx.bot.send_message(uid, test, parse_mode="Markdown", disable_web_page_preview=True)
    users[uid]["free"] -= 1
    save_all()

async def pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if len(ctx.args) != 1:
        await update.message.reply_text("Usage: `/pay YOUR_TXID`", parse_mode="Markdown")
        return
    txid = ctx.args[0].strip().lower()
    pending_payments[txid] = uid
    await update.message.reply_text(f"TXID `{txid}` recorded. Checking...", parse_mode="Markdown")
    save_all()

async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/stats username`")
        return
    inf = ctx.args[0].lower()
    if inf not in tracker:
        await update.message.reply_text(f"No data for **{inf}**.")
        return
    s = tracker[inf]
    rev = s["subs"] * PRICE
    cut = rev * COMMISSION_RATE
    conv = s["subs"] / max(s["joins"], 1) * 100
    await update.message.reply_text(
        f"**{inf.upper()} STATS**\n"
        f"Joins: `{s['joins']}`\n"
        f"Subs: `{s['subs']}`\n"
        f"Revenue: **${rev:.2f}**\n"
        f"You Earn: **${cut:.2f}**\n"
        f"Conv: `{conv:.1f}%`",
        parse_mode="Markdown"
    )

async def owner(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != YOUR_ADMIN_ID:
        return
    total_subs = sum(t["subs"] for t in tracker.values())
    revenue = total_subs * PRICE
    top = sorted(tracker.items(), key=lambda x: x[1]["subs"] * PRICE, reverse=True)[:10]
    top_list = "\n".join([f"{i+1}. {n} → ${s['subs']*PRICE:.2f}" for i, (n, s) in enumerate(top)])
    await update.message.reply_text(
        f"**OWNER**\n"
        f"Subs: `{total_subs}`\n"
        f"Revenue: **${revenue:.2f}**\n"
        f"Top:\n{top_list or 'None'}",
        parse_mode="Markdown"
    )

# === RUG CHECK (GoPlus) ===
async def is_safe(addr: str, chain: str, session: aiohttp.ClientSession) -> bool:
    chain_id = 56 if chain == "BSC" else 1  # BSC=56, Solana=1 (GoPlus supports)
    url = GOPLUS_API.format(chain_id=chain_id, addr=addr.lower())
    try:
        async with session.get(url, timeout=8) as r:
            if r.status != 200: return False
            res = await r.json()
            info = res.get("result", {}).get(addr.lower(), {})
            return (
                info.get("is_open_source") in ("1", 1) and
                info.get("honeypot") in ("0", 0) and
                info.get("can_take_back_ownership") not in ("1", 1)
            )
    except:
        return False

# === SCANNER: NEW + OLD PAIRS ===
async def scanner(app: Application):
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                now = time.time()
                for chain, url in [("SOL", "solana"), ("BSC", "bsc")]:
                    # 1. NEW PAIRS
                    async with s.get(f"https://api.dexscreener.com/latest/dex/new-pairs/{url}", timeout=15) as r:
                        if r.status == 200:
                            data = await r.json()
                            for p in data.get("pairs", [])[:30]:
                                await process_pair(p, chain, url, s, app, now)

                    # 2. OLD/TRENDING PAIRS (skip top 30)
                    async with s.get(f"https://api.dexscreener.com/latest/dex/search?q={url}", timeout=15) as r:
                        if r.status == 200:
                            data = await r.json()
                            for p in data.get("pairs", [])[30:80]:
                                await process_pair(p, chain, url, s, app, now)

                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"SCANNER ERROR: {e}")
                await asyncio.sleep(60)

# === PROCESS PAIR ===
async def process_pair(p, chain, url, s, app, now):
    base = p.get("baseToken", {})
    addr = base.get("address")
    pair_addr = p.get("pairAddress")
    if not addr or not pair_addr or addr in seen:
        return

    liq = p.get("liquidity", {}).get("usd", 0)
    fdv = p.get("fdv", 0)
    vol = p.get("volume", {}).get("m5", 0)
    sym = base.get("symbol", "??")[:16]

    # RUG CHECK
    if not await is_safe(addr, chain, s):
        return

    # VOLUME SPIKE
    h = vol_hist[addr]; h.append(vol)
    spike = vol / (sum(h)/len(h)) if len(h) > 1 else 1
    volume_spike = spike >= 1.5

    # WHALE BUY
    whale_buy = False
    try:
        async with s.get(f"https://api.dexscreener.com/latest/dex/pairs/{url}/{pair_addr}", timeout=10) as r:
            if r.status == 200:
                data = await r.json()
                buys = data.get("pair", {}).get("txns", {}).get("m5", {}).get("buys", [])
                whale_buy = any(b.get("total", 0) >= 2000 for b in buys[:5])
    except:
        pass

    # COOLDOWN
    if addr in last_alerted and now - last_alerted[addr] < 300:
        return

    # FILTERS (REALISTIC)
    trigger_base = liq >= 5000 and fdv >= 10000 and vol >= 800
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
                    if d["free"] > 0: d["free"] -= 1
                    sent += 1
                    if sent % 8 == 0: await asyncio.sleep(1)
                except:
                    pass
        logger.info(f"ALERT: {sym} | {reason_str} | {sent} users")
        save_all()

# === PAYMENT CHECKER ===
async def check_payments(app: Application):
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                url = "https://api.bscscan.com/api"
                params = {
                    "module": "account", "action": "tokentx",
                    "contractaddress": USDT_CONTRACT,
                    "address": WALLETS["BSC"],
                    "sort": "desc"
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
                                    inf = users[uid]["source"].split("_", 1)[1] if users[uid]["source"].startswith("track_") else None
                                    if inf and inf in tracker:
                                        tracker[inf]["subs"] += 1
                                        tracker[inf]["revenue"] += PRICE
                                    await app.bot.send_message(uid, "Payment confirmed! Unlimited alerts ON.")
                                    logger.info(f"PAID: {uid}")
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

    logger.info("ONION ALERTS LIVE — NEW + OLD PAIRS + RUG CHECK")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        save_all()
        logger.info("Shutdown.")
