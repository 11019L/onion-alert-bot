# main.py - ONION ALERTS: 1 TEST + 3 REAL ALPHA CAs (FREE TRIAL)
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

FREE_ALERTS = 4  # 1 test + 3 real
PRICE_USD = 29.99
YOUR_BSC_WALLET = "0xYourBscWalletHere"  # CHANGE TO YOUR WALLET
YOUR_SOL_WALLET = "YourSolanaWalletHere"  # CHANGE TO YOUR WALLET
# =============

logging.basicConfig(level=logging.INFO)
users = {}
volume_hist = defaultdict(lambda: deque(maxlen=5))
seen_tokens = set()

# === /start: WELCOME + 1 TEST CA ===
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
        f"Free trial: {free} alerts (1 test + 3 real)\n"
        f"Subscribe: ${PRICE_USD}/mo → YOUR WALLET\n\n"
        f"**Pay USDT + Memo:** `{payment_id}`\n"
        f"BSC: `{YOUR_BSC_WALLET}`\n"
        f"Solana: `{YOUR_SOL_WALLET}`"
    )

    # SEND 1 TEST CA (uses 1 alert)
    test_msg = (
        f"**ALPHA SOL**\n"
        f"`ONION`\n"
        f"**CA:** `3pzgwrwusc5d92pbjqltwlqxnyrq86jkbf2ov3fh4dpd`\n"
        f"Liq: $1,800 | FDV: $3,100\n"
        f"5m Vol: $1,200\n"
        f"[DexScreener](https://dexscreener.com/solana/3pzgwrwusc5d92pbjqltwlqxnyrq86jkbf2ov3fh4dpd)"
    )
    try:
        await context.bot.send_message(user_id, test_msg, parse_mode="Markdown", disable_web_page_preview=True)
        users[user_id]["free_left"] -= 1
        print(f"TEST CA SENT TO {user_id}")
    except Exception as e:
        print(f"TEST SEND ERROR: {e}")

# === REAL ALPHA SCANNER (3 REAL CAs) ===
async def alpha_scanner(app):
    import aiohttp
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                print("SCANNING FOR REAL ALPHA...")
                async with session.get("https://api.dexscreener.com/latest/dex/search/?q=solana") as resp:
                    if resp.status != 200:
                        await asyncio.sleep(60)
                        continue
                    data = await resp.json()
                    pairs = data.get("pairs", [])

                    for pair in pairs:
                        base = pair.get("baseToken", {})
                        addr = base.get("address", "")
                        if not addr or addr in seen_tokens: 
                            continue

                        liq = pair.get("liquidity", {}).get("usd", 0)
                        fdv = pair.get("fdv", 0)
                        vol5m = pair.get("volume", {}).get("m5", 0)
                        symbol = base.get("symbol", "UNKNOWN")

                        # === ALPHA FILTERS ===
                        if liq <= 0 or liq > 25000: continue
                        if fdv > 200000: continue
                        if vol5m < 1000: continue
                        
                        hist = volume_hist[addr]
                        hist.append(vol5m)
                        if len(hist) < 2: continue
                        avg = sum(hist) / len(hist)
                        if vol5m < 2 * avg: continue

                        # === SEND TO USERS (3 REAL) ===
                        seen_tokens.add(addr)
                        msg = (
                            f"**REAL ALPHA SOL**\n"
                            f"`{symbol}`\n"
                            f"**CA:** `{addr}`\n"
                            f"Liq: ${liq:,.0f} | FDV: ${fdv:,.0f}\n"
                            f"5m Vol: ${vol5m:,.0f} (↑{vol5m/avg:.1f}x)\n"
                            f"[DexScreener](https://dexscreener.com/solana/{addr})"
                        )

                        sent = 0
                        for uid in list(users.keys()):
                            data = users[uid]
                            if data["free_left"] > 0 or data.get("subscribed_until"):
                                try:
                                    await app.bot.send_message(uid, msg, parse_mode="Markdown", disable_web_page_preview=True)
                                    print(f"REAL ALPHA SENT TO {uid}")
                                    sent += 1
                                    if data["free_left"] > 0:
                                        data["free_left"] -= 1
                                except: pass
                        print(f"{sent} USERS GOT REAL ALPHA: {symbol}")

                await asyncio.sleep(60)
            except Exception as e:
                print(f"SCANNER ERROR: {e}")
                await asyncio.sleep(60)

# === START BOT ===
app = Application.builder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))

async def main():
    asyncio.create_task(alpha_scanner(app))
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    print("ONION ALERTS LIVE — 1 TEST + 3 REAL")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
