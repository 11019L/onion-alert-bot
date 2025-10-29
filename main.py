# main.py - FULL WORKING BOT: TEST CA + REAL CAs EVERY 5 MINS
import os
import asyncio
import json
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta

from telegram.ext import Application, CommandHandler, ContextTypes

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("ERROR: Add BOT_TOKEN in Railway Variables!")
    exit()

FREE_ALERTS = 3
PRICE_USD = 29.99
YOUR_BSC_WALLET = "0xYourBscWalletHere"  # CHANGE TO YOUR WALLET
YOUR_SOL_WALLET = "YourSolanaWalletHere"  # CHANGE TO YOUR WALLET
# =============

logging.basicConfig(level=logging.INFO)
users = {}

# === /start COMMAND ===
async def start(update: ContextTypes.DEFAULT_TYPE, context):
    user_id = update.effective_user.id
    username = update.effective_user.username or "Anon"
    
    if user_id not in users:
        users[user_id] = {"free_left": FREE_ALERTS, "subscribed_until": None, "username": username}
        print(f"NEW USER: {user_id} ({username})")

    free = users[user_id]["free_left"]
    payment_id = f"PAY_{user_id}_{int(datetime.utcnow().timestamp())}"
    
    await update.message.reply_text(
        f"ONION ALERTS\n\n"
        f"Free trial: {free} alerts left\n"
        f"Subscribe: ${PRICE_USD}/mo\n\n"
        f"**Pay USDT + Memo:** `{payment_id}`\n"
        f"BSC: `{YOUR_BSC_WALLET}`\n"
        f"Solana: `{YOUR_SOL_WALLET}`"
    )

    # SEND TEST CA
    await send_test_ca(context.application)

# === TEST CA ===
async def send_test_ca(app):
    await asyncio.sleep(5)
    msg = (
        f"**TEST SOL TOKEN**\n"
        f"`TESTCOIN`\n"
        f"**CA:** `test123456789abcdefghi`\n"
        f"Liq: $5,000 | FDV: $50,000\n"
        f"5m Vol: $10,000\n"
        f"[DexScreener](https://dexscreener.com/solana/test123456789abcdefghi)"
    )
    for uid in list(users.keys()):
        try:
            await app.bot.send_message(uid, msg, parse_mode="Markdown", disable_web_page_preview=True)
            print(f"TEST CA SENT TO {uid}")
            if users[uid]["free_left"] > 0:
                users[uid]["free_left"] -= 1
        except Exception as e:
            print(f"TEST SEND ERROR: {e}")

# === POLL SCANNER — REAL CAs EVERY 5 MINS ===
async def poll_scanner(app):
    import aiohttp
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                print("POLLING DEXSCREENER...")
                async with session.get("https://api.dexscreener.com/latest/dex/search/?q=solana") as resp:
                    if resp.status != 200:
                        print(f"API ERROR: {resp.status}")
                        await asyncio.sleep(60)
                        continue
                    data = await resp.json()
                    pairs = data.get("pairs", [])
                    print(f"FOUND {len(pairs)} PAIRS")

                    for pair in pairs[:5]:
                        base = pair.get("baseToken", {})
                        token = {
                            "addr": base.get("address", "UNKNOWN"),
                            "symbol": base.get("symbol", "UNKNOWN"),
                            "liq": pair.get("liquidity", {}).get("usd", 0),
                            "fdv": pair.get("fdv", 0),
                            "vol5m": pair.get("volume", {}).get("m5", 0)
                        }

                        msg = (
                            f"**NEW SOL TOKEN**\n"
                            f"`{token['symbol']}`\n"
                            f"**CA:** `{token['addr']}`\n"
                            f"Liq: ${token['liq']:,.0f} | FDV: ${token['fdv']:,.0f}\n"
                            f"5m Vol: ${token['vol5m']:,.0f}\n"
                            f"[DexScreener](https://dexscreener.com/solana/{token['addr']})"
                        )

                        sent = 0
                        for uid in list(users.keys()):
                            try:
                                await app.bot.send_message(uid, msg, parse_mode="Markdown", disable_web_page_preview=True)
                                print(f"REAL CA SENT TO {uid}: {token['symbol']}")
                                sent += 1
                                if users[uid]["free_left"] > 0:
                                    users[uid]["free_left"] -= 1
                            except Exception as e:
                                print(f"SEND ERROR: {e}")
                        print(f"→ {sent} USERS GOT ALERT")

                await asyncio.sleep(300)  # Every 5 mins
            except Exception as e:
                print(f"POLL CRASH: {e}")
                await asyncio.sleep(60)

# === START BOT ===
app = Application.builder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))

async def main():
    asyncio.create_task(poll_scanner(app))  # REAL CAs
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    print("BOT LIVE — TYPE /start IN TELEGRAM")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
