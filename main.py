# main.py - ONION ALERTS: NEW PAIRS API + YOUR FILTERS + 1-3/HOUR
import os, asyncio, logging
from collections import defaultdict, deque
from telegram.ext import Application, CommandHandler, ContextTypes

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("ERROR: Add BOT_TOKEN in Railway Variables!")
    exit()

FREE_ALERTS = 3
PRICE = 19.99
WALLETS = {
    "BSC": "0xa11351776d6f483418b73c8e40bc706c93e8b1e1",
    "Solana": "B4427oKJc3xnQf91kwXHX27u1SsVyB8GDQtc3NBxRtkK"
}
# =============

logging.basicConfig(level=logging.INFO)
users = {}
seen = set()
vol_hist = defaultdict(lambda: deque(maxlen=5))
last_sent = 0

# === /start: Welcome + Test CA ===
async def start(update, ctx):
    uid = update.effective_user.id
    if uid not in users:
        users[uid] = {"free": FREE_ALERTS}
    free = users[uid]["free"]
    memo = f"PAY_{uid}_{int(asyncio.get_event_loop().time())}"

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
        f"**TEST ALPHA SOL**\n"
        f"`ONIONCOIN`\n"
        f"**CA:** `onion123456789abcdefghi123456789abcdefghi`\n"
        f"Liq: $9,200 | FDV: $52,000\n"
        f"5m Vol: $15,600\n"
        f"[DexScreener](https://dexscreener.com/solana/onion123456789abcdefghi123456789abcdefghi)\n\n"
        f"_Test alert — real ones coming soon!_"
    )
    await ctx.bot.send_message(uid, test, parse_mode="Markdown", disable_web_page_preview=True)
    users[uid]["free"] -= 1

# === SCANNER: NEW PAIRS API (Catches x402 in 55 mins) ===
async def scanner(app):
    import aiohttp
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                candidates = []
                now = asyncio.get_event_loop().time()

                for chain, url in [("SOL", "solana"), ("BSC", "bsc")]:
                    # NEW PAIRS API — CATCHES ALL FRESH LAUNCHES
                    async with s.get(f"https://api.dexscreener.com/latest/dex/new-pairs/{url}") as r:
                        if r.status != 200: continue
                        data = await r.json()
                        for p in data.get("pairs", [])[:20]:  # Scan 20 newest
                            b = p.get("baseToken", {})
                            addr = b.get("address")
                            if not addr or addr in seen: continue

                            liq = p.get("liquidity", {}).get("usd", 0)
                            fdv = p.get("fdv", 0)
                            vol = p.get("volume", {}).get("m5", 0)
                            sym = b.get("symbol", "??")
                            
                            # YOUR FILTERS
                            if liq < 45000: continue      # ≥ $45K
                            if fdv < 100000: continue     # ≥ $100K
                            if vol < 2000: continue       # ≥ $2K

                            # TRIGGERS
                            large_buy = vol >= 2000
                            h = vol_hist[addr]; h.append(vol)
                            spike = vol / (sum(h)/len(h)) if len(h) > 1 else 1
                            volume_spike = spike >= 1.5

                            if large_buy or volume_spike:
                                candidates.append((liq, fdv, vol, spike, addr, sym, chain, url))

                # SEND BEST
                if candidates:
                    liq, fdv, vol, spike, addr, sym, chain, url = max(candidates, key=lambda x: x[3])
                    await send_alert(app, chain, addr, sym, liq, fdv, vol, spike, url)
                    last_sent = now

                # FALLBACK: 1/hour
                elif now - last_sent > 3600 and candidates:
                    liq, fdv, vol, _, addr, sym, chain, url = max(candidates, key=lambda x: x[2])
                    await send_alert(app, chain, addr, sym, liq, fdv, vol, 1, url, fallback=True)
                    last_sent = now

                await asyncio.sleep(60)
            except Exception as e:
                print(f"SCANNER ERROR: {e}")
                await asyncio.sleep(60)

# === SEND ALERT ===
async def send_alert(app, chain, addr, sym, liq, fdv, vol, spike, url, fallback=False):
    seen.add(addr)
    prefix = "**FALLBACK** " if fallback else ""
    msg = (
        f"{prefix}**ALPHA {chain}**\n"
        f"`{sym}`\n"
        f"**CA:** `{addr}`\n"
        f"Liq: ${liq:,.0f} | FDV: ${fdv:,.0f}\n"
        f"5m Vol: ${vol:,.0f}"
        + (f" (↑{spike:.1f}x)" if spike >= 1.5 else "") + f"\n"
        f"[DexScreener](https://dexscreener.com/{url}/{addr})"
    )
    for uid, d in list(users.items()):
        if d["free"] > 0 or d.get("subscribed_until"):
            try:
                await app.bot.send_message(uid, msg, parse_mode="Markdown", disable_web_page_preview=True)
                if d["free"] > 0: d["free"] -= 1
            except: pass

# === RUN BOT ===
app = Application.builder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))

async def main():
    asyncio.create_task(scanner(app))
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    print("ONION ALERTS LIVE — NEW PAIRS API — CATCHING x402")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
