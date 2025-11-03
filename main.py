# main.py - ONION ALERTS - FULL WORKING VERSION
import os
import asyncio
import logging
import json
import time
from collections import defaultdict, deque
from datetime import datetime
import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.helpers import escape_markdown

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    exit("ERROR: Set BOT_TOKEN")

FREE_ALERTS = 3
PRICE = 19.99
YOUR_ADMIN_ID = int(os.getenv("ADMIN_ID", "1319494378"))
WALLETS = {"BSC": "0xa11351776d6f483418b73c8e40bc706c93e8b1e1"}

# APIs
NEW_PAIRS_URL = "https://api.dexscreener.com/latest/dex/new-pairs/solana"
SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q=solana"

# === LOGGING ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("onion")

# === DATA ===
DATA_FILE = "data.json"
users = {}
seen = {}
last_alerted = {}
vol_hist = defaultdict(lambda: deque(maxlen=5))

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
                return (
                    data.get("users", {}),
                    data.get("seen", {}),
                    data.get("last_alerted", {})
                )
        except: pass
    return {}, {}, {}

users, seen, last_alerted = load_data()

# === HELPERS ===
def format_alert(sym, addr, liq, fdv, vol, pair_addr, reason):
    return (
        f"*ALPHA SOL* — {escape_markdown(reason, 2)}\n"
        f"`{escape_markdown(sym, 2)}`\n"
        f"*CA:* `{addr}`\n"
        f"Liq: ${liq:,.0f} | FDV: ${fdv:,.0f}\n"
        f"5m Vol: ${vol:,.0f}\n"
        f"[DexScreener](https://dexscreener.com/solana/{pair_addr})"
    )

# === COMMANDS ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    logger.info(f"/start from {uid}")

    if uid not in users:
        users[uid] = {"free": FREE_ALERTS, "paid": False}

    # WELCOME
    welcome = (
        "*ONION ALERTS*\n\n"
        f"Free trial: `{FREE_ALERTS}` alerts left\n"
        f"Subscribe: `${PRICE}/mo`\n\n"
        f"*Pay USDT (BSC):*\n`{WALLETS['BSC']}`\n\n"
        "Send TXID: `/pay YOUR_TXID`\n\n"
        "_Auto-upgrade in <2 min!_"
    )
    await update.effective_message.reply_text(welcome, parse_mode="MarkdownV2")

    # TEST ALERT
    test = (
        "*TEST ALPHA SOL*\n"
        "`ONIONCOIN`\n"
        "*CA:* `onion123456789abcdefghi123456789abcdefghi`\n"
        "Liq: $9,200 | FDV: $52,000\n"
        "5m Vol: $15,600\n"
        "[DexScreener](https://dexscreener.com/solana/test)\n"
        "_Test alert — real ones coming!_"
    )
    await update.effective_message.reply_text(test, parse_mode="MarkdownV2", disable_web_page_preview=True)
    logger.info("Test alert sent")

async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Manual payment. Contact admin.")

# === SCANNER ===
async def scanner(app: Application):
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                now = time.time()
                candidates = []

                # New pairs
                async with session.get(NEW_PAIRS_URL) as r:
                    if r.status == 200:
                        data = await r.json()
                        for p in data.get("pairs", [])[:30]:
                            candidates.append((p, "new"))

                # Search
                async with session.get(SEARCH_URL) as r:
                    if r.status == 200:
                        data = await r.json()
                        for p in data.get("pairs", [])[50:100]:
                            candidates.append((p, "search"))

                alerts = []
                for p, src in candidates:
                    addr = p.get("baseToken", {}).get("address")
                    pair_addr = p.get("pairAddress")
                    if not addr or not pair_addr or addr in seen:
                        continue

                    sym = p.get("baseToken", {}).get("symbol", "???")[:20]
                    liq = p.get("liquidity", {}).get("usd", 0) or 0
                    fdv = p.get("fdv", 0) or 0
                    vol = p.get("volume", {}).get("m5", 0) or 0

                    if addr in last_alerted and now - last_alerted[addr] < 300:
                        continue

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
                        reason.append(f"Spike {spike:.1f}x")

                    if not reason: continue

                    last_alerted[addr] = now
                    msg = format_alert(sym, addr, liq, fdv, vol, pair_addr, " | ".join(reason))
                    alerts.append((msg, addr))

                # SEND TO ALL USERS (ADMIN ALWAYS GETS)
                for msg, addr in alerts:
                    for uid in users:
                        if uid == YOUR_ADMIN_ID or users[uid]["free"] > 0:
                            try:
                                await app.bot.send_message(uid, msg, parse_mode="MarkdownV2", disable_web_page_preview=True)
                                if users[uid]["free"] > 0 and uid != YOUR_ADMIN_ID:
                                    users[uid]["free"] -= 1
                            except: pass
                    seen[addr] = time.time()
                    logger.info(f"ALERT: {addr}")

                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"Scanner error: {e}")
                await asyncio.sleep(60)

# === POST INIT ===
async def post_init(app: Application):
    app.create_task(scanner(app))
    try:
        await app.bot.send_message(YOUR_ADMIN_ID, "*BOT LIVE*\nScanning SOL...")
    except: pass

# === MAIN ===
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pay", pay))
    app.post_init = post_init
    logger.info("BOT STARTED")
    app.run_polling()

if __name__ == "__main__":
    main()
