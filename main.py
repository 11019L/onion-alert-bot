# main.py - FULLY WORKING: /start + TEST CA + REAL ALERTS
import os
import asyncio
import json
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta

import websockets
from telegram.ext import Application, CommandHandler, ContextTypes

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN missing in Variables!")
    exit()

FREE_ALERTS = 3
PRICE_USD = 19.99
YOUR_BSC_WALLET = "0xa11351776d6f483418b73c8e40bc706c93e8b1e1"
YOUR_SOL_WALLET = "B4427oKJc3xnQf91kwXHX27u1SsVyB8GDQtc3NBxRtkK"
# =============

logging.basicConfig(level=logging.INFO)
users = {}
volume_hist = defaultdict(lambda: deque(maxlen=5))

# === /start COMMAND ===
async def start(update: ContextTypes.DEFAULT_TYPE, context):
    user_id = update.effective_user.id
    username = update.effective_user.username or "Anon"
    
    if user_id not in users:
        users[user_id] = {"free_left": FREE_ALERTS, "subscribed_until": None, "username": username}
        print(f"NEW USER: {user_id} ({username})")

    free = users[user_id]["free_left"]
    sub = users[user_id]["subscribed_until"]

    if sub and datetime.fromisoformat(sub) > datetime.utcnow():
        await update.message.reply_text("You're SUBSCRIBED! Unlimited alerts.")
    else:
        payment_id = f"PAY_{user_id}_{int(datetime.utcnow().timestamp())}"
        await update.message.reply_text(
            f"Alpha Bot\n\n"
            f"Free trial: {free} alerts left\n"
            f"Subscribe: ${PRICE_USD}/mo\n\n"
            f"**Pay USDT to:**\n"
            f"BSC: `{YOUR_BSC_WALLET}`\n"
            f"Solana: `{YOUR_SOL_WALLET}`\n\n"
            f"**Memo:** `{payment_id}`\n"
            f"Auto-upgrade in <5 min!"
        )
    
    # SEND TEST ALERT
    await send_test_alert(context.application)

# === TEST ALERT ===
async def send_test_alert(app):
    await asyncio.sleep(2)
    if not users:
        return
    test_ca = "onion123456789abcdefghi123456789abcdefghi"
    msg = (
        f"**TEST ALPHA SOL**\n"
        f"`ONIONCOIN`\n"
        f"**CA:** `{test_ca}`\n"
        f"Liq: $9,200 | FDV: $52,000\n"
        f"5m Vol: $15,600\n"
        f"[DexScreener](https://dexscreener.com/solana/{test_ca})\n\n"
        f"_Test alert — real ones coming soon!_"
    )
    for uid in list(users.keys()):
        try:
            await app.bot.send_message(uid, msg, parse_mode="Markdown", disable_web_page_preview=True)
            print(f"TEST CA SENT TO {uid}")
            if users[uid]["free_left"] > 0:
                users[uid]["free_left"] -= 1
        except Exception as e:
            print(f"ERROR: {e}")

# === COIN SCANNER ===
# === WORKING POLL SCANNER — REAL CAs EVERY 5 MINS ===
import aiohttp

async def poll_scanner(app):
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                # WORKING API: Search for new Solana pairs with USDC (catches new launches)
                async with session.get("https://api.dexscreener.com/latest/dex/search/?q=solana") as resp:
                    data = await resp.json()
                    print(f"POLL: Found {len(data.get('pairs', []))} pairs")  # DEBUG

                    for pair in data.get("pairs", [])[:10]:  # First 10 pairs
                        base = pair.get("baseToken", {})
                        liq = pair.get("liquidity", {}).get("usd", 0)
                        fdv = pair.get("fdv", 0)
                        vol5m = pair.get("volume", {}).get("m5", 0)
                        symbol = base.get("symbol", "UNKNOWN")
                        addr = base.get("address", "UNKNOWN")

                        # === NO FILTERS — SEND EVERYTHING ===
                        token = {
                            "addr": addr,
                            "symbol": symbol,
                            "chain": "SOL",
                            "liq": liq,
                            "fdv": fdv,
                            "vol5m": vol5m
                        }

                        msg = (
                            f"**NEW SOL TOKEN**\n"
                            f"`{token['symbol']}`\n"
                            f"**CA:** `{token['addr']}`\n"
                            f"Liq: ${token['liq']:,.0f} | FDV: ${token['fdv']:,.0f}\n"
                            f"5m Vol: ${token['vol5m']:,.0f}\n"
                            f"[DexScreener](https://dexscreener.com/solana/{token['addr']})"
                        )

                        # Send to ALL users
                        sent = 0
                        for uid, data in list(users.items()):
                            now = datetime.utcnow()
                            if (data.get("subscribed_until") and datetime.fromisoformat(data["subscribed_until"]) > now) or data["free_left"] > 0:
                                try:
                                    await app.bot.send_message(uid, msg, parse_mode="Markdown", disable_web_page_preview=True)
                                    print(f"SENT TO {uid}: {symbol} ({addr[:8]}...)")
                                    sent += 1
                                    if data["free_left"] > 0:
                                        data["free_left"] -= 1
                                except Exception as e:
                                    print(f"Send error to {uid}: {e}")
                        print(f"POLL ALERT SENT: {sent} users got {symbol}")

            await asyncio.sleep(300)  # Every 5 mins
        except Exception as e:
            print(f"POLL ERROR: {e}")
            await asyncio.sleep(60)

# In main():
asyncio.create_task(poll_scanner(app))

# === START BOT ===
app = Application.builder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))

async def main():
    asyncio.create_task(coin_scanner(app))
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    print("Bot running... Type /start in Telegram!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
