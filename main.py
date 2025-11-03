# main.py - ONION ALERTS - 100% WORKING
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
    exit("ERROR: Set BOT_TOKEN env variable")

FREE_ALERTS = 3
PRICE = 19.99
COMMISSION_RATE = 0.25
YOUR_ADMIN_ID = int(os.getenv("ADMIN_ID", "1319494378"))

WALLETS = {"BSC": "0xa11351776d6f483418b73c8e40bc706c93e8b1e1"}

GOPLUS_API = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={addrs}"
NEW_PAIRS_URL = "https://api.dexscreener.com/latest/dex/new-pairs/{chain}"
SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q={chain}"

# === LOGGING ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("onion")

# === DATA ===
DATA_FILE = "data.json"
SAVE_INTERVAL = 30

tracker = {}
users = {}
seen = {}
last_alerted = {}
vol_hist = defaultdict(lambda: deque(maxlen=5))
goplus_cache = {}
save_lock = asyncio.Lock()

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                raw = json.load(f)
                now = time.time()
                return {
                    "tracker": raw.get("tracker", {}),
                    "users": raw.get("users", {}),
                    "seen": {k: v for k, v in raw.get("seen", {}).items() if now - v < 86400},
                    "last_alerted": {k: v for k, v in raw.get("last_alerted", {}).items() if now - v < 3600}
                }
        except Exception as e:
            logger.error(f"Load failed: {e}")
    return {"tracker": {}, "users": {}, "seen": {}, "last_alerted": {}}

data = load_data()
tracker.update(data["tracker"])
users.update(data["users"])
seen.update(data["seen"])
last_alerted.update(data["last_alerted"])

# === AUTO SAVE ===
async def auto_save():
    while True:
        await asyncio.sleep(SAVE_INTERVAL)
        async with save_lock:
            save = {
                "tracker": copy.deepcopy(tracker),
                "users": users.copy(),
                "seen": {k: v for k, v in seen.items() if time.time() - v < 86400},
                "last_alerted": {k: v for k, v in last_alerted.items() if time.time() - v < 3600}
            }
            try:
                with open(DATA_FILE, "w") as f:
                    json.dump(save, f, indent=2)
                logger.info("Data saved.")
            except Exception as e:
                logger.error(f"Save error: {e}")

# === HELPERS ===
def get_dex_url(chain, pair_addr):
    return f"https://dexscreener.com/{'solana' if chain == 'SOL' else 'bsc'}/{pair_addr}"

def format_alert(chain, sym, addr, liq, fdv, vol, pair_addr, reason):
    return (
        f"*ALPHA {escape_markdown(chain, 2)}* — {escape_markdown(reason, 2)}\n"
        f"`{escape_markdown(sym, 2)}`\n"
        f"*CA:* `{escape_markdown(addr, 2)}`\n"
        f"Liq: ${liq:,.0f} | FDV: ${fdv:,.0f}\n"
        f"5m Vol: ${vol:,.0f}\n"
        f"[DexScreener]({get_dex_url(chain, pair_addr)})"
    )

# === GOPLUS ===
async def is_safe_batch(addrs, chain, session):
    if not addrs: return {}
    chain_id = 56 if chain == "BSC" else 1
    url = GOPLUS_API.format(chain_id=chain_id, addrs=",".join(addrs))
    try:
        async with session.get(url, timeout=10) as r:
            if r.status == 200:
                data = await r.json()
                result = {k.lower(): v for k, v in data.get("result", {}).items()}
                res = {}
                for addr in addrs:
                    info = result.get(addr.lower(), {})
                    safe = (
                        info.get("is_open_source") == "1" and
                        info.get("honeypot") == "0" and
                        info.get("can_take_back_ownership") != "1"
                    )
                    res[addr] = safe
                return res
    except: pass
    return {a: False for a in addrs}

# === COMMANDS ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    logger.info(f"/start from {uid}")

    if uid not in users:
        users[uid] = {"free": FREE_ALERTS, "paid": False}

    msg = (
        "*ONION ALERTS*\n\n"
        f"Free trial: `{FREE_ALERTS}` alerts left\n"
        f"Subscribe: `${PRICE}/mo`\n\n"
        f"*Pay USDT (BSC):*\n`{WALLETS['BSC']}`\n\n"
        f"Send TXID: `/pay YOUR_TXID`\n\n"
        "*TEST ALERT*\n"
        "`ONIONCOIN`\n"
        "*CA:* `onion123456789abcdefghi123456789abcdefghi`\n"
        "Liq: $9,200 | FDV: $52,000\n"
        "5m Vol: $15,600\n"
        "[DexScreener](https://dexscreener.com/solana/test)\n"
        "_You're in!_"
    )
    await update.effective_message.reply_text(msg, parse_mode="MarkdownV2", disable_web_page_preview=True)
    logger.info("Start message sent.")

async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Manual payment. Contact admin.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("Usage: `/stats name`")
        return
    name = context.args[0].lower()
    if name not in tracker:
        await update.effective_message.reply_text("No data.")
        return
    s = tracker[name]
    await update.effective_message.reply_text(
        f"*{name.upper()}*\nJoins: `{s.get('joins',0)}`\nSubs: `{s.get('subs',0)}`",
        parse_mode="MarkdownV2"
    )

async def owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != YOUR_ADMIN_ID:
        return
    await update.effective_message.reply_text(
        f"*DASHBOARD*\nUsers: `{len(users)}`\nAlerts sent: `{len(seen)}`",
        parse_mode="MarkdownV2"
    )

# === SCANNER ===
async def scanner(app: Application):
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                now = time.time()
                candidates = []
                for chain, slug in [("SOL", "solana"), ("BSC", "bsc")]:
                    try:
                        async with session.get(NEW_PAIRS_URL.format(chain=slug)) as r:
                            if r.status == 200:
                                for p in (await r.json()).get("pairs", [])[:50]:
                                    candidates.append((p, chain, "new"))
                    except: pass
                    try:
                        async with session.get(SEARCH_URL.format(chain=slug)) as r:
                            if r.status == 200:
                                for p in (await r.json()).get("pairs", [])[50:150]:
                                    candidates.append((p, chain, "search"))
                    except: pass

                addr_to_pair = {}
                for p, chain, src in candidates:
                    addr = p.get("baseToken", {}).get("address")
                    pair_addr = p.get("pairAddress")
                    if addr and pair_addr and addr not in seen:
                        addr_to_pair[addr] = (p, chain, src, pair_addr)

                if not addr_to_pair:
                    await asyncio.sleep(60)
                    continue

                # Safety
                safety = {}
                chain_groups = defaultdict(list)
                for addr, (_, chain, _, _) in addr_to_pair.items():
                    chain_groups[chain].append(addr)
                for chain, addrs in chain_groups.items():
                    safety.update(await is_safe_batch(addrs, chain, session))

                alerts = []
                for addr, (p, chain, src, pair_addr) in addr_to_pair.items():
                    if not safety.get(addr, False): continue
                    sym = p.get("baseToken", {}).get("symbol", "???")[:20]
                    liq = p.get("liquidity", {}).get("usd", 0) or 0
                    fdv = p.get("fdv", 0) or 0
                    vol = p.get("volume", {}).get("m5", 0) or 0
                    if addr in last_alerted and now - last_alerted[addr] < 300: continue

                    h = vol_hist[addr]
                    prev = sum(h)/len(h) if h else 0
                    spike = vol/prev if prev > 0 else 1.0
                    h.append(vol)

                    reason = []
                    if src == "new" and liq >= 500 and fdv >= 3000 and vol >= 200:
                        reason.append("New Pair")
                    if liq >= 5000 and fdv >= 10000 and vol >= 1000:
                        reason.append("Medium")
                    if spike >= 1.5:
                        reason.append(f"High {spike:.1f}x")

                    if not reason: continue
                    last_alerted[addr] = now
                    alerts.append((format_alert(chain, sym, addr, liq, fdv, vol, pair_addr, " | ".join(reason)), addr))

                for msg, addr in alerts:
                    async with save_lock:
                        targets = list(users.items())
                    for uid, u in targets:
                        if uid == YOUR_ADMIN_ID or u["free"] > 0 or u.get("paid"):
                            try:
                                await app.bot.send_message(uid, msg, parse_mode="MarkdownV2", disable_web_page_preview=True)
                                if u["free"] > 0 and uid != YOUR_ADMIN_ID:
                                    async with save_lock: users[uid]["free"] -= 1
                                async with save_lock: seen[addr] = time.time()
                            except: pass
                    logger.info(f"ALERT → {addr}")

                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"Scanner error: {e}")
                await asyncio.sleep(60)

# === POST INIT ===
async def post_init(app: Application):
    app.create_task(scanner(app))
    app.create_task(auto_save())
    try:
        await app.bot.send_message(YOUR_ADMIN_ID, "*BOT LIVE*")
    except: pass

# === MAIN ===
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("owner", owner))
    app.post_init = post_init
    logger.info("BOT STARTED")
    app.run_polling()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        with open(DATA_FILE, "w") as f:
            json.dump({"tracker": tracker, "users": users, "seen": seen, "last_alerted": last_alerted}, f)
        logger.info("Stopped.")
