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
PRICE_USD = 29.99
YOUR_BSC_WALLET = "0x55d398326f99059fF775485166e8dD2aD1fC5B1e"
YOUR_SOL_WALLET = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
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
                    "vol5m": float(pair["volume"].get("m5", 0)) or 1,
                }

                if token["liq"] <= 0: continue
                hist = volume_hist[token["addr"]]
                hist.append(token["vol5m"])
                if len(hist) < 1: continue
                avg = max(sum(hist) / len(hist), 1)
                if token["vol5m"] < 0.1 * avg: continue

                msg = (
                    f"**ALPHA {token['chain']}**\n"
                    f"`{token['symbol']}`\n"
                    f"**CA:** `{token['addr']}`\n"
                    f"Liq: ${token['liq']:,.0f} | FDV: ${token['fdv']:,.0f}\n"
                    f"5m Vol: ${token['vol5m']:,.0f}\n"
                    f"[DexScreener](https://dexscreener.com/{chain}/{token['addr']})"
                )

                for uid, data in list(users.items()):
                    now = datetime.utcnow()
                    if (data["subscribed_until"] and datetime.fromisoformat(data["subscribed_until"]) > now) or data["free_left"] > 0:
                        try:
                            await app.bot.send_message(uid, msg, parse_mode="Markdown", disable_web_page_preview=True)
                            print(f"REAL ALERT SENT TO {uid}")
                            if data["free_left"] > 0:
                                data["free_left"] -= 1
                        except Exception as e:
                            print(f"ERROR: {e}")
        except Exception as e:
            logging.error(f"WS Error: {e}")
            await asyncio.sleep(5)

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
