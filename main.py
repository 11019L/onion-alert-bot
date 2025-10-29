# main.py - SIMPLE ALPHA BOT
import os
import asyncio
import json
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta

import websockets
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN")  # Will come from Secrets
FREE_ALERTS = 3
PRICE_USD = 29.99
# =============

logging.basicConfig(level=logging.INFO)

users = {}
volume_hist = defaultdict(lambda: deque(maxlen=5))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in users:
        users[user_id] = {"free_left": FREE_ALERTS, "subscribed_until": None}

    user = users[user_id]
    free = user["free_left"]
    sub = user["subscribed_until"]

    if sub and datetime.fromisoformat(sub) > datetime.utcnow():
        await update.message.reply_text(
            "You're SUBSCRIBED! Unlimited alpha alerts.")
    else:
        await update.message.reply_text(
            f"Alpha Alerts Bot\n\n"
            f"Free trial: {free} alerts left\n"
            f"Subscribe: ${PRICE_USD}/month\n\n"
            f"Send USDT to @YourUsername and type /paid")


async def paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    users[user_id] = {
        "free_left": 0,
        "subscribed_until":
        (datetime.utcnow() + timedelta(days=30)).isoformat()
    }
    await update.message.reply_text("SUBSCRIBED! You now get all alpha coins.")


# === SCANNER ===
async def scanner():
    async for ws in websockets.connect("wss://stream.dexscreener.com/ws"):
        try:
            await ws.send(
                json.dumps({
                    "method": "SUBSCRIBE",
                    "params": ["newPairs"],
                    "id": 1
                }))
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

                if token["liq"] > 8000 or token["fdv"] > 100000: continue
                hist = volume_hist[token["addr"]]
                hist.append(token["vol5m"])
                if len(hist) < 2: continue
                avg = sum(hist) / len(hist)
                if token["vol5m"] < 6 * avg: continue

                msg = (
                    f"**ALPHA {token['chain']}**\n"
                    f"`{token['symbol']}` • `{token['addr'][:8]}…`\n"
                    f"Liq: `${token['liq']:,.0f}` | FDV: `${token['fdv']:,.0f}`\n"
                    f"5m Vol: `${token['vol5m']:,.0f}`\n"
                    f"[DexScreener](https://dexscreener.com/{chain}/{token['addr']})"
                )

                for uid, data in list(users.items()):
                    now = datetime.utcnow()
                    if (data["subscribed_until"] and datetime.fromisoformat(
                            data["subscribed_until"])
                            > now) or data["free_left"] > 0:
                        try:
                            await app.bot.send_message(
                                uid,
                                msg,
                                parse_mode="Markdown",
                                disable_web_page_preview=True)
                            if data["free_left"] > 0:
                                data["free_left"] -= 1
                        except:
                            pass
        except:
            await asyncio.sleep(5)


# === START ===
app = Application.builder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("paid", paid))


async def main():
    asyncio.create_task(scanner())
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    print("Bot is running... Send /start to begin")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
