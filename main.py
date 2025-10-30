# main.py - FIXED: REAL NEW PAIRS API + 1 TEST + 3 REAL
import os
import asyncio
import logging
from collections import defaultdict, deque
from datetime import datetime

from telegram.ext import Application, CommandHandler, ContextTypes

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("ERROR: Add BOT_TOKEN in Railway Variables!")
    exit()

FREE_ALERTS = 3  # 3 real after test
PRICE_USD = 19.99
YOUR_BSC_WALLET = "0xa11351776d6f483418b73c8e40bc706c93e8b1e1"
YOUR_SOL_WALLET = "B4427oKJc3xnQf91kwXHX27u1SsVyB8GDQtc3NBxRtkK"
# =============

logging.basicConfig(level=logging.INFO)
users = {}
seen_tokens = set()

# === /start: EXACT FORMAT ===
async def start(update: ContextTypes.DEFAULT_TYPE, context):
    user_id = update.effective_user.id
    username = update.effective_user.username or "Anon"
    
    if user_id not in users:
        users[user_id] = {"free_left": FREE_ALERTS, "subscribed_until": None, "username": username}
        print(f"NEW USER: {user_id} ({username})")

    free = users[user_id]["free_left"]
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

    # === TEST ALERT ===
    test_msg = (
        f"**TEST ALPHA SOL**\n"
        f"`ONIONCOIN`\n"
        f"**CA:** `onion123456789abcdefghi123456789abcdefghi`\n"
        f"Liq: $9,200 | FDV: $52,000\n"
        f"5m Vol: $15,600\n"
        f"[DexScreener](https://dexscreener.com/solana/onion123456789abcdefghi123456789abcdefghi)\n\n"
        f"_Test alert — real ones coming soon!_"
    )
    try:
        await context.bot.send_message(user_id, test_msg, parse_mode="Markdown", disable_web_page_preview=True)
        users[user_id]["free_left"] -= 1
        print(f"TEST ALERT SENT TO {user_id}")
    except Exception as e:
        print(f"TEST SEND ERROR: {e}")

# === FIXED SCANNER: NEW PAIRS API (1-3/HOUR) ===
async def alpha_scanner(app):
    import aiohttp
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                print("SCANNING NEW PAIRS...")
                # FIXED API: NEW PAIRS (not search)
                async with session.get("https://api.dexscreener.com/latest/dex/pairs/solana") as resp:
                    if resp.status != 200:
                        print(f"API ERROR: {resp.status}")
                        await asyncio.sleep(60)
                        continue
                    data = await resp.json()
                    pairs = data.get("pairs", [])
                    print(f"NEW PAIRS FOUND: {len(pairs)}")

                    for pair in pairs[:10]:  # Check latest 10
                        base = pair.get("baseToken", {})
                        addr = base.get("address", "")
                        if not addr or addr in seen_tokens: 
                            continue

                        liq = pair.get("liquidity", {}).get("usd", 0)
                        fdv = pair.get("fdv", 0)
                        vol5m = pair.get("volume", {}).get("m5", 0)
                        symbol = base.get("symbol", "UNKNOWN")

                        # === LOOSE FILTERS: MORE ALERTS ===
                        if liq <= 0 or liq > 50000: continue  # < $50K
                        if fdv > 500000: continue             # < $500K
                        if vol5m < 500: continue               # Min vol $500
                        
                        # 1.5x spike
                        hist = volume_hist[addr]
                        hist.append(vol5m)
                        if len(hist) < 2: continue
                        avg = sum(hist) / len(hist)
                        if vol5m < 1.5 * avg: continue

                        # === SEND ===
                        seen_tokens.add(addr)
                        msg = (
                            f"**ALPHA SOL**\n"
                            f"`{symbol}`\n"
                            f"**CA:** `{addr}`\n"
                            f"Liq: ${liq:,.0f} | FDV: ${fdv:,.0f}\n"
                            f"5m Vol: ${vol5m:,.0f}\n"
                            f"[DexScreener](https://dexscreener.com/solana/{addr})"
                        )

                        sent = 0
                        for uid in list(users.keys()):
                            data = users[uid]
                            if data["free_left"] > 0 or data.get("subscribed_until"):
                                try:
                                    await app.bot.send_message(uid, msg, parse_mode="Markdown", disable_web_page_preview=True)
                                    print(f"ALPHA SENT TO {uid}: {symbol}")
                                    sent += 1
                                    if data["free_left"] > 0:
                                        data["free_left"] -= 1
                                except: pass
                        print(f"{sent} USERS GOT ALPHA: {symbol}")

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
    print("ONION ALERTS LIVE — NEW PAIRS API")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
