# main.py - ONION ALERTS + 25% AUTO COMMISSION (NO ADMIN ID)
import os, asyncio, logging, json, aiohttp
from collections import defaultdict, deque
from telegram.ext import Application, CommandHandler, ContextTypes

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN: exit("ERROR: Add BOT_TOKEN")

FREE_ALERTS = 3
PRICE = 19.99
COMMISSION_RATE = 0.25  # 25%
WALLETS = {
    "BSC": "0xa11351776d6f483418b73c8e40bc706c93e8b1e1",
    "Solana": "B4427oKJc3xnQf91kwXHX27u1SsVyB8GDQtc3NBxRtkK"
}
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY", "")  # Optional
# =============

logging.basicConfig(level=logging.INFO)

# Persistent data
try:
    with open("tracker.json", "r") as f:
        tracker = json.load(f)
except:
    tracker = {}

users = {}
seen = set()
vol_hist = defaultdict(lambda: deque(maxlen=5))
last_sent = 0
pending_memos = {}

def save_tracker():
    with open("tracker.json", "w") as f:
        json.dump(tracker, f)

# /start
async def start(update, ctx):
    uid = update.effective_user.id
    source = ctx.args[0] if ctx.args and ctx.args[0].startswith("track_") else "organic"
    influencer = source.split("_", 1)[1] if "_" in source else None

    if influencer:
        if influencer not in tracker:
            tracker[influencer] = {"joins": 0, "subs": 0, "revenue": 0.0}
        tracker[influencer]["joins"] += 1
        save_tracker()

    if uid not in users:
        users[uid] = {"free": FREE_ALERTS, "source": source, "paid": False}
    free = users[uid]["free"]
    memo = f"PAY_{uid}_{int(asyncio.get_event_loop().time())}"
    pending_memos[memo] = uid

    await update.message.reply_text(
        f"Alpha Bot\n\n"
        f"Free trial: {free} alerts left\n"
        f"Subscribe: ${PRICE}/mo\n\n"
        f"**Pay USDT to:**\n"
        f"BSC: `{WALLETS['BSC']}`\n"
        f"Solana: `{WALLETS['Solana']}`\n\n"
        f"**Memo:** `{memo}`\n"
        f"Auto-upgrade in <5 min!"
    )

    test = (
        f"**TEST ALPHA SOL**\n`ONIONCOIN`\n**CA:** `onion123456789abcdefghi123456789abcdefghi`\n"
        f"Liq: $9,200 | FDV: $52,000\n5m Vol: $15,600\n"
        f"[DexScreener](https://dexscreener.com/solana/onion123456789abcdefghi123456789abcdefghi)\n\n"
        f"_Test alert — real ones coming soon!_"
    )
    await ctx.bot.send_message(uid, test, parse_mode="Markdown", disable_web_page_preview=True)
    users[uid]["free"] -= 1

# /stats
# /stats — Clean, no payout note
async def stats(update, ctx):
    if not ctx.args:
        await update.message.reply_text("Usage: /stats [yourusername]")
        return
    influencer = ctx.args[0].lower()
    if influencer not in tracker:
        await update.message.reply_text(f"No data for **{influencer}**.")
        return

    stats = tracker[influencer]
    revenue = stats["subs"] * PRICE
    your_cut = revenue * COMMISSION_RATE
    conv = stats["subs"] / max(stats["joins"], 1) * 100

    await update.message.reply_text(
        f"**{influencer.upper()} STATS**\n\n"
        f"Joins: `{stats['joins']}`\n"
        f"Paid Subs: `{stats['subs']}`\n"
        f"Total Revenue: **${revenue:.2f}**\n"
        f"**You Earn: ${your_cut:.2f}** (25%)\n"
        f"Conversion: `{conv:.1f}%`",
        parse_mode="Markdown"
    )

# === AUTO PAYMENT ===
async def check_payments(app):
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                url = f"https://api.bscscan.com/api?module=account&action=tokentx&contractaddress=0x55d398326f99059fF775485246999027B3197955&address={WALLETS['BSC']}&sort=desc&apikey={BSCSCAN_API_KEY}"
                async with s.get(url) as r:
                    if r.status != 200: continue
                    data = await r.json()
                    for tx in data.get("result", [])[:10]:
                        if tx["to"].lower() != WALLETS["BSC"].lower(): continue
                        value = int(tx["value"]) / 1e18
                        if 19.9 <= value <= 20.1:  # ~$19.99
                            memo = tx.get("input", "")[-64:]
                            if memo in pending_memos:
                                uid = pending_memos[memo]
                                await confirm_payment(uid, app)
                                del pending_memos[memo]
                await asyncio.sleep(30)
            except Exception as e:
                print(f"PAYMENT ERROR: {e}")
                await asyncio.sleep(30)

async def confirm_payment(uid, app):
    if uid not in users or users[uid]["paid"]: return
    users[uid]["paid"] = True
    source = users[uid]["source"]
    influencer = source.split("_", 1)[1] if source.startswith("track_") else None

    if influencer and influencer in tracker:
        tracker[influencer]["subs"] += 1
        tracker[influencer]["revenue"] += PRICE
        save_tracker()

    try:
        await app.bot.send_message(uid, "Payment confirmed! Unlimited alerts ON.")
    except: pass

# === SCANNER & SEND ALERT (unchanged) ===
# ... [Your existing scanner + send_alert code here] ...

# === RUN ===
app = Application.builder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("stats", stats))

async def main():
    asyncio.create_task(scanner(app))
    asyncio.create_task(check_payments(app))
    await app.initialize(); await app.start(); await app.updater.start_polling()
    print("ONION ALERTS LIVE — 25% AUTO")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
