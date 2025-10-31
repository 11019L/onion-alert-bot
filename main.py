# main.py - ONION ALERTS + INFLUENCER + AUTO PAY + OWNER
import os, asyncio, logging, json
import aiohttp  # ← WORKS BECAUSE DOCKER INSTALLED IT
from collections import defaultdict, deque
from telegram.ext import Application, CommandHandler, ContextTypes

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("ERROR: Add BOT_TOKEN in environment variables!")
    exit()

FREE_ALERTS = 3
PRICE = 19.99
COMMISSION_RATE = 0.25
YOUR_ADMIN_ID = 1319494378  # ← CHANGE TO YOUR TELEGRAM ID

WALLETS = {
    "BSC": "0xa11351776d6f483418b73c8e40bc706c93e8b1e1",
    "Solana": "B4427oKJc3xnQf91kwXHX27u1SsVyB8GDQtc3NBxRtkK"
}
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY", "")

logging.basicConfig(level=logging.INFO)

# Load tracker
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
        f"Alpha Bot\n\nFree trial: {free} alerts left\nSubscribe: ${PRICE}/mo\n\n"
        f"**Pay USDT to:**\nBSC: `{WALLETS['BSC']}`\nSolana: `{WALLETS['Solana']}`\n\n"
        f"**Memo:** `{memo}`\nAuto-upgrade in <5 min!"
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

# /owner
async def owner(update, ctx):
    if update.effective_user.id != YOUR_ADMIN_ID:
        return

    total_influencers = len(tracker)
    total_joins = sum(t["joins"] for t in tracker.values())
    total_subs = sum(t["subs"] for t in tracker.values())
    total_revenue = total_subs * PRICE
    owner_profit = total_revenue * (1 - COMMISSION_RATE)
    top = sorted(tracker.items(), key=lambda x: x[1]["subs"] * PRICE, reverse=True)[:10]
    top_list = "\n".join([f"{i+1}. {name} → ${stats['subs']*PRICE:.2f} ({stats['subs']} subs)" 
                         for i, (name, stats) in enumerate(top)])

    await update.message.reply_text(
        f"**OWNER DASHBOARD**\n\n"
        f"Total Influencers: `{total_influencers}`\n"
        f"Total Joins: `{total_joins}`\n"
        f"Total Subs: `{total_subs}`\n"
        f"Total Revenue: **${total_revenue:.2f}**\n"
        f"Your Profit: **${owner_profit:.2f}**\n\n"
        f"**TOP INFLUENCERS**\n{top_list or 'None'}",
        parse_mode="Markdown"
    )

# === SCANNER (NEW PAIRS) ===
async def scanner(app):
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                candidates = []
                now = asyncio.get_event_loop().time()

                for chain, url in [("SOL", "solana"), ("BSC", "bsc")]:
                    async with s.get(f"https://api.dexscreener.com/latest/dex/new-pairs/{url}") as r:
                        if r.status != 200: continue
                        data = await r.json()
                        for p in data.get("pairs", [])[:20]:
                            b = p.get("baseToken", {})
                            addr = b.get("address")
                            if not addr or addr in seen: continue

                            liq = p.get("liquidity", {}).get("usd", 0)
                            fdv = p.get("fdv", 0)
                            vol = p.get("volume", {}).get("m5", 0)
                            sym = b.get("symbol", "??")
                            
                            if liq < 45000: continue
                            if fdv < 100000: continue
                            if vol < 2000: continue

                            h = vol_hist[addr]; h.append(vol)
                            spike = vol / (sum(h)/len(h)) if len(h) > 1 else 1
                            if vol >= 2000 or spike >= 1.5:
                                candidates.append((liq, fdv, vol, spike, addr, sym, chain, url))

                if candidates:
                    liq, fdv, vol, spike, addr, sym, chain, url = max(candidates, key=lambda x: x[3])
                    await send_alert(app, chain, addr, sym, liq, fdv, vol, spike, url)
                    last_sent = now

                await asyncio.sleep(60)
            except Exception as e:
                print(f"SCANNER ERROR: {e}")
                await asyncio.sleep(60)

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
                        if 19.9 <= value <= 20.1:
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

# === SEND ALERT ===
async def send_alert(app, chain, addr, sym, liq, fdv, vol, spike, url):
    seen.add(addr)
    msg = (
        f"**ALPHA {chain}**\n`{sym}`\n**CA:** `{addr}`\n"
        f"Liq: ${liq:,.0f} | FDV: ${fdv:,.0f}\n5m Vol: ${vol:,.0f}"
        + (f" (↑{spike:.1f}x)" if spike >= 1.5 else "") + f"\n"
        f"[DexScreener](https://dexscreener.com/{url}/{addr})"
    )
    for uid, d in list(users.items()):
        if d["free"] > 0 or d.get("paid", False):
            try:
                await app.bot.send_message(uid, msg, parse_mode="Markdown", disable_web_page_preview=True)
                if d["free"] > 0: d["free"] -= 1
            except: pass

# === RUN ===
app = Application.builder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("stats", stats))
app.add_handler(CommandHandler("owner", owner))

async def main():
    asyncio.create_task(scanner(app))
    asyncio.create_task(check_payments(app))
    await app.initialize(); await app.start(); await app.updater.start_polling()
    print("ONION ALERTS LIVE — DOCKER + AUTO")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
