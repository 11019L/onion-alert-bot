# main.py - AUTO PAYMENTS + NOTIFICATIONS
import os
import asyncio
import json
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta
import requests  # For API calls

import websockets
from telegram.ext import Application, CommandHandler, ContextTypes

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 1319494378  # ← YOUR ADMIN ID HERE (from @userinfobot)
YOUR_BSC_WALLET = "0xa11351776d6f483418b73c8e40bc706c93e8b1e1"  # ← YOUR BSC USDT RECEIVER
YOUR_SOL_WALLET = "B4427oKJc3xnQf91kwXHX27u1SsVyB8GDQtc3NBxRtkK"  # ← YOUR SOL USDT RECEIVER
BSCSCAN_API_KEY = "YourFreeBscScanKey"  # ← Get free at bscscan.com/apis (optional)
FREE_ALERTS = 3
PRICE_USD = 19.99
# =============

logging.basicConfig(level=logging.INFO)
users = {}
pending_payments = {}  # user_id → payment_id
volume_hist = defaultdict(lambda: deque(maxlen=5))

async def start(update, context):
    user_id = update.effective_user.id
    username = update.effective_user.username or "NoUsername"
    if user_id not in users:
        users[user_id] = {"free_left": FREE_ALERTS, "subscribed_until": None, "username": username}

    user = users[user_id]
    free = user["free_left"]
    sub = user["subscribed_until"]

    if sub and datetime.fromisoformat(sub) > datetime.utcnow():
        await update.message.reply_text("You're SUBSCRIBED! Unlimited alpha alerts.")
    else:
        # Generate unique payment ID
        payment_id = f"PAY_{user_id}_{datetime.utcnow().timestamp():.0f}"
        pending_payments[user_id] = payment_id

        await update.message.reply_text(
            f"Alpha Alerts Bot\n\n"
            f"Free trial: {free} alerts left\n"
            f"Subscribe: ${PRICE_USD}/month\n\n"
            f"**Pay with USDT:**\n"
            f"BSC: `{YOUR_BSC_WALLET}`\n"
            f"Solana: `{YOUR_SOL_WALLET}`\n\n"
            f"**INCLUDE THIS MEMO:** `{payment_id}`\n\n"
            f"Bot auto-detects & upgrades in <5 min!"
        )

async def paid(update, context):  # Manual fallback
    user_id = update.effective_user.id
    users[user_id] = {
        "free_left": 0,
        "subscribed_until": (datetime.utcnow() + timedelta(days=30)).isoformat(),
        "username": update.effective_user.username or "NoUsername"
    }
    await update.message.reply_text("SUBSCRIBED! (Manual upgrade)")

# === AUTO PAYMENT SCANNER ===
async def payment_scanner(app):
    while True:
        try:
            # Scan BSC (example - add Solana similar)
            if BSCSCAN_API_KEY:
                url = f"https://api.bscscan.com/api?module=account&action=tokentx&address={YOUR_BSC_WALLET}&contractaddress=0x55d398326f99059fF775485166e8dD2aD1fC5B1e&startblock=0&endblock=99999999&sort=desc&apikey={BSCSCAN_API_KEY}"
                resp = requests.get(url)
                txns = resp.json().get("result", [])
                for txn in txns[:5]:  # Last 5 txns
                    value_usd = float(txn.get("value", 0)) / 10**18 * PRICE_USD  # Approx USD
                    if value_usd >= PRICE_USD - 1:  # Close enough
                        memo = txn.get("input", "") or txn.get("data", "")  # Memo in input
                        if "PAY_" in memo:
                            # Match to user
                            for uid, pid in list(pending_payments.items()):
                                if pid in memo:
                                    # UPGRADE USER
                                    users[uid] = {
                                        "free_left": 0,
                                        "subscribed_until": (datetime.utcnow() + timedelta(days=30)).isoformat(),
                                        "username": users.get(uid, {}).get("username", "Unknown")
                                    }
                                    del pending_payments[uid]

                                    # NOTIFY YOU (ADMIN)
                                    await app.bot.send_message(
                                        ADMIN_ID,
                                        f"🚨 **PAYMENT DETECTED!** 🚨\n"
                                        f"User: @{users[uid]['username']} (ID: {uid})\n"
                                        f"Amount: ~${value_usd:.2f} USDT\n"
                                        f"Memo: {pid}\n"
                                        f"Upgraded to premium! 🎉"
                                    )

                                    # NOTIFY USER
                                    await app.bot.send_message(
                                        uid,
                                        "✅ **SUBSCRIBED!** Payment confirmed. Unlimited alerts activated for 30 days! 🚀"
                                    )
                                    break
            await asyncio.sleep(60)  # Check every 1 min
        except Exception as e:
            logging.error(f"Payment scan error: {e}")
            await asyncio.sleep(60)

# === COIN SCANNER (unchanged) ===
async def coin_scanner(app):
    async for ws in websockets.connect("wss://stream.dexscreener.com/ws"):
        try:
            await ws.send(json.dumps({"method": "SUBSCRIBE", "params": ["newPairs"], "id": 1}))
            async for msg in ws:
                data = json.loads(msg)
                if data.get("method") != "newPair": continue
                pair = data["params"]["pair"]
                chain = pair["chainId"]
                if chain not in ["solana", "bsc"]: continue

                base = pair["baseToken"]
                token = {
                    "addr": base["address"],
                    "symbol": base["symbol"],
                    "chain": "SOL" if chain == "solana" else "BSC",
                    "liq": float(pair["liquidity"].get("usd", 0)),
                    "fdv": float(pair.get("fdv", 0)),
                    "vol5m": float(pair["volume"].get("m5", 0)),
                }

                # === ULTRA LOOSE FILTERS FOR TESTING ===
                if token["liq"] <= 0: continue
                hist = volume_hist[token["addr"]]
                hist.append(token["vol5m"] or 1)
                if len(hist) < 1: continue
                avg = max(sum(hist) / len(hist), 1)
                if token["vol5m"] < 0.1 * avg: continue

                msg = (
                    f"**ALPHA {token['chain']}**\n"
                    f"`{token['symbol']}`\n"
                    f"**CA:** `{token['addr']}`\n"
                    f"Liq: `${token['liq']:,.0f}` | FDV: `${token['fdv']:,.0f}`\n"
                    f"5m Vol: `${token['vol5m']:,.0f}`\n"
                    f"[DexScreener](https://dexscreener.com/{chain}/{token['addr']})"
                )

                for uid, data in list(users.items()):
                    now = datetime.utcnow()
                    if (data["subscribed_until"] and datetime.fromisoformat(data["subscribed_until"]) > now) or data["free_left"] > 0:
                        try:
                            await app.bot.send_message(uid, msg, parse_mode="Markdown", disable_web_page_preview=True)
                            if data["free_left"] > 0:
                                data["free_left"] -= 1
                        except: pass
        except Exception as e:
            logging.error(f"WS Error: {e}")
            await asyncio.sleep(5)

# === START BOT ===
app = Application.builder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("paid", paid))

async def main():
    asyncio.create_task(coin_scanner(app))
    asyncio.create_task(payment_scanner(app))
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    print("Bot is running... Auto-payments enabled!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())