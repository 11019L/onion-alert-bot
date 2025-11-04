#!/usr/bin/env python3
import os
import asyncio
import json
import time
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any

import aiohttp
import requests  # for BSCScan & Solana RPC
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)
from telegram.helpers import escape_markdown

# --------------------------------------------------------------------------- #
#                               CONFIGURATION                               #
# --------------------------------------------------------------------------- #
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
FREE_ALERTS = 3
PRICE_USDT = 19.99
COMMISSION_RATE = 0.25
WALLETS = {"BSC": os.getenv("WALLET_BSC", "0xa11351776d6f483418b73c8e40bc706c93e8b1e1")}

# APIs
GOPLUS_API = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={addrs}"
NEW_PAIRS_URL = "https://api.dexscreener.com/latest/dex/new-pairs/{chain}"
SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q={chain}"

# RPC
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
BSCSCAN_API = "https://api.bscscan.com/api"

# Persistence
DATA_FILE = Path("data.json")
SAVE_INTERVAL = 30

# --------------------------------------------------------------------------- #
#                                 LOGGING                                   #
# --------------------------------------------------------------------------- #
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("onion")

# --------------------------------------------------------------------------- #
#                               PERSISTENCE                                 #
# --------------------------------------------------------------------------- #
def load_data() -> dict:
    if DATA_FILE.is_file():
        try:
            raw = json.loads(DATA_FILE.read_text())
            now = time.time()
            seen = {k: v for k, v in raw.get("seen", {}).items() if now - v < 86_400}
            last = {k: v for k, v in raw.get("last_alerted", {}).items() if now - v < 3_600}
            return {
                "tracker": raw.get("tracker", {}),
                "users": raw.get("users", {}),
                "seen": seen,
                "last_alerted": last,
                "token_state": raw.get("token_state", {}),  # new
            }
        except Exception as exc:
            log.error(f"Failed to load {DATA_FILE}: {exc}")
    return {
        "tracker": {}, "users": {}, "seen": {}, "last_alerted": {}, "token_state": {}
    }

def save_data(data: dict):
    tmp = {
        "tracker": data["tracker"],
        "users": data["users"],
        "seen": data["seen"],
        "last_alerted": data["last_alerted"],
        "token_state": data["token_state"],
    }
    try:
        DATA_FILE.write_text(json.dumps(tmp, indent=2))
    except Exception as exc:
        log.error(f"Failed to save data: {exc}")

data = load_data()
tracker = data["tracker"]
users = data["users"]
seen = data["seen"]
last_alerted = data["last_alerted"]
token_state = data["token_state"]  # {addr: {"sent_levels": [], "last_vol": 0}}

vol_hist: defaultdict[str, deque] = defaultdict(lambda: deque(maxlen=5))
goplus_cache: dict[str, tuple[bool, float]] = {}
save_lock = asyncio.Lock()

# --------------------------------------------------------------------------- #
#                               AUTO-SAVE                                   #
# --------------------------------------------------------------------------- #
async def auto_save():
    while True:
        await asyncio.sleep(SAVE_INTERVAL)
        async with save_lock:
            save_data(data)
            log.debug("Data auto-saved")

# --------------------------------------------------------------------------- #
#                               HELPERS                                     #
# --------------------------------------------------------------------------- #
def dex_url(chain: str, pair_addr: str) -> str:
    slug = "solana" if chain == "SOL" else "bsc"
    return f"https://dexscreener.com/{slug}/{pair_addr}"

def safe_md(txt) -> str:
    return escape_markdown(str(txt), version=2)

def format_alert(chain, sym, addr, liq, fdv, vol, pair_addr, level):
    level_emoji = {"min": "Min", "medium": "Medium", "max": "Max", "large_buy": "SNIPE", "upgrade": "UPGRADED"}.get(level, level.upper())
    return (
        f"*{level_emoji} ALERT* [{safe_md(chain)}]\n"
        f"`{safe_md(sym)}`\n"
        f"*CA:* `{safe_md(addr)}`\n"
        f"Liq: ${liq:,.0f} | FDV: ${fdv:,.0f}\n"
        f"5m Vol: ${vol:,.0f}\n"
        f"[DexScreener]({dex_url(chain, pair_addr)})"
    )

# --------------------------------------------------------------------------- #
#                               RUG CHECK                                   #
# --------------------------------------------------------------------------- #
async def is_safe_batch(addrs: list[str], chain: str, session: aiohttp.ClientSession) -> dict[str, bool]:
    if not addrs:
        return {}
    chain_id = 56 if chain == "BSC" else 1
    addrs_str = ",".join(addrs)
    url = GOPLUS_API.format(chain_id=chain_id, addrs=addrs_str)

    now = time.time()
    cached = {a: goplus_cache[a][0] for a in addrs if a in goplus_cache and now - goplus_cache[a][1] < 3_600}
    to_check = [a for a in addrs if a not in cached]
    results = cached.copy()

    if not to_check:
        return results

    try:
        async with session.get(url, timeout=10) as resp:
            if resp.status != 200:
                raise ValueError(f"GoPlus HTTP {resp.status}")
            payload = await resp.json()
            for addr in to_check:
                info = payload.get("result", {}).get(addr.lower(), {})
                safe = (
                    info.get("is_open_source") == "1"
                    and info.get("honeypot") == "0"
                    and info.get("can_take_back_ownership") != "1"
                )
                results[addr] = safe
                goplus_cache[addr] = (safe, now)
    except Exception as exc:
        log.warning(f"GoPlus batch failed: {exc}")
        for addr in to_check:
            results[addr] = False
    return results

# --------------------------------------------------------------------------- #
#                            LARGE BUY DETECTION                             #
# --------------------------------------------------------------------------- #
async def detect_large_buy(addr: str, chain: str) -> bool:
    if chain == "SOL":
        try:
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getSignaturesForAddress",
                "params": [addr, {"limit": 20}]
            }
            resp = requests.post(SOLANA_RPC, json=payload, timeout=8).json()
            sigs = resp.get("result", [])
            for sig in sigs[:5]:
                tx = requests.post(SOLANA_RPC, json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTransaction",
                    "params": [sig["signature"], {"encoding": "jsonParsed"}]
                }, timeout=8).json()
                if tx.get("result"):
                    pre = tx["result"]["meta"]["preBalances"][0]
                    post = tx["result"]["meta"]["postBalances"][0]
                    sol_in = (pre - post) / 1e9
                    if sol_in * 180 > 2000:  # >$2k at $180/SOL
                        return True
        except:
            pass
    elif chain == "BSC":
        try:
            url = f"{BSCSCAN_API}?module=account&action=tokentx&contractaddress={addr}&page=1&offset=10"
            resp = requests.get(url, timeout=8).json()
            for tx in resp.get("result", []):
                value = int(tx.get("value", "0")) / 1e6
                if value > 2000:
                    return True
        except:
            pass
    return False

# --------------------------------------------------------------------------- #
#                               FILTER LOGIC                                #
# --------------------------------------------------------------------------- #
def get_alert_level(liq, fdv, vol, is_new_pair, volume_spike, large_buy):
    if is_new_pair and liq >= 1000 and fdv >= 10000 and vol >= 500:
        return "min"
    if liq >= 25000 and fdv >= 70000 and vol >= 3000:
        return "medium"
    if large_buy and volume_spike:
        return "max"
    return None

# --------------------------------------------------------------------------- #
#                               COMMANDS                                    #
# --------------------------------------------------------------------------- #
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    args = ctx.args or []
    source = args[0] if args and args[0].startswith("track_") else "organic"
    influencer = source.split("_", 1)[1] if "_" in source else None

    if influencer and influencer not in tracker:
        tracker[influencer] = {"joins": 0, "subs": 0, "revenue": 0.0}
    if influencer:
        tracker[influencer]["joins"] += 1

    if uid not in users:
        users[uid] = {"free": FREE_ALERTS, "source": source, "paid": False, "paid_until": None}
    free_left = users[uid]["free"]

    msg = (
        f"*ONION ALERTS*\n\n"
        f"Free trial: `{free_left}` alerts left\n"
        f"Subscribe: `${PRICE_USDT}/mo`\n\n"
        f"*Pay USDT (BSC):*\n`{WALLETS['BSC']}`\n\n"
        f"After payment send TXID:\n`/pay YOUR_TXID`\n"
        f"_Auto-upgrade in <2 min!_"
    )
    await update.message.reply_text(msg, parse_mode="MarkdownV2")

    # TEST ALERT — DOES NOT COUNT
    test = (
        f"*TEST ALERT*\n"
        f"`ONIONCOIN`\n"
        f"*CA:* `onion123456789abcdefghi123456789abcdefghi`\n"
        f"Liq: $9,200 | FDV: $52,000\n"
        f"5m Vol: $15,600\n"
        f"[DexScreener](https://dexscreener.com/solana/onion123456789abcdefghi123456789abcdefghi)\n\n"
        f"_Test alert — does NOT use a free trial_"
    )
    await ctx.bot.send_message(uid, test, parse_mode="MarkdownV2", disable_web_page_preview=True)

async def pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/pay <TXID>`", parse_mode="MarkdownV2")
        return
    txid = ctx.args[0].strip()
    uid = update.effective_user.id

    try:
        url = f"{BSCSCAN_API}?module=account&action=tokentx&address={WALLETS['BSC']}&page=1&offset=20"
        resp = requests.get(url, timeout=10).json()
        for tx in resp.get("result", []):
            if tx.get("hash", "").lower() == txid.lower() and tx.get("tokenSymbol") == "USDT":
                value = float(tx.get("value", "0")) / 1e6
                if value >= PRICE_USDT:
                    users[uid]["paid"] = True
                    users[uid]["paid_until"] = (datetime.utcnow() + timedelta(days=30)).isoformat()
                    users[uid]["free"] = 0
                    await update.message.reply_text("*Payment confirmed!* Premium active for 30 days.", parse_mode="MarkdownV2")
                    src = users[uid].get("source", "")
                    if src.startswith("track_"):
                        inf = src.split("_", 1)[1]
                        if inf in tracker:
                            tracker[inf]["subs"] += 1
                            tracker[inf]["revenue"] += PRICE_USDT
                    return
    except:
        pass
    await update.message.reply_text("TXID not found or amount insufficient.")

async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/stats <your_username>`", parse_mode="MarkdownV2")
        return
    inf = ctx.args[0].lower()
    if inf not in tracker:
        await update.message.reply_text(f"No data for **{safe_md(inf)}**.", parse_mode="MarkdownV2")
        return
    s = tracker[inf]
    joins = s.get("joins", 0)
    subs = s.get("subs", 0)
    rev = subs * PRICE_USDT
    cut = rev * COMMISSION_RATE
    conv = subs / max(joins, 1) * 100
    await update.message.reply_text(
        f"*{safe_md(inf.upper())} STATS*\n\n"
        f"Joins: `{joins}`\nPaid Subs: `{subs}`\n"
        f"Revenue: *${rev:.2f}*\n"
        f"You Earn: *${cut:.2f}* (25%)\n"
        f"Conversion: `{conv:.1f}%`",
        parse_mode="MarkdownV2",
    )

async def owner(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    total_inf = len(tracker)
    total_joins = sum(t["joins"] for t in tracker.values())
    total_subs = sum(t["subs"] for t in tracker.values())
    total_rev = total_subs * PRICE_USDT
    profit = total_rev * (1 - COMMISSION_RATE)
    top = sorted(tracker.items(), key=lambda x: x[1]["subs"] * PRICE_USDT, reverse=True)[:10]
    top_txt = "\n".join(
        f"{i+1}. {safe_md(name)} → ${stats['subs']*PRICE_USDT:.2f} ({stats['subs']} subs)"
        for i, (name, stats) in enumerate(top)
    ) or "None"
    await update.message.reply_text(
        f"*OWNER DASHBOARD*\n\n"
        f"Influencers: `{total_inf}`\n"
        f"Joins: `{total_joins}`\n"
        f"Subs: `{total_subs}`\n"
        f"Revenue: *${total_rev:.2f}*\n"
        f"Your Profit: *${profit:.2f}*\n\n"
        f"*TOP INFLUENCERS*\n{top_txt}",
        parse_mode="MarkdownV2",
    )

# --------------------------------------------------------------------------- #
#                               SCANNER                                     #
# --------------------------------------------------------------------------- #
async def scanner(app: Application):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        while True:
            try:
                now = time.time()
                candidates = []

                for chain, slug in [("SOL", "solana"), ("BSC", "bsc")]:
                    try:
                        async with session.get(NEW_PAIRS_URL.format(chain=slug), timeout=10) as r:
                            if r.status == 200:
                                for p in (await r.json()).get("pairs", [])[:50]:
                                    candidates.append((p, chain, slug, "new"))
                    except Exception as e:
                        log.warning(f"New pairs {chain} error: {e}")

                    try:
                        async with session.get(SEARCH_URL.format(chain=slug), timeout=10) as r:
                            if r.status == 200:
                                for p in (await r.json()).get("pairs", [])[50:150]:
                                    candidates.append((p, chain, slug, "search"))
                    except Exception as e:
                        log.warning(f"Search {chain} error: {e}")

                if not candidates:
                    await asyncio.sleep(60)
                    continue

                addr_to_pair = {}
                for p, chain, slug, src in candidates:
                    base = p.get("baseToken", {})
                    addr = base.get("address")
                    pair_addr = p.get("pairAddress")
                    if not addr or not pair_addr or addr in seen:
                        continue
                    addr_to_pair[addr] = (p, chain, slug, src == "new", pair_addr)

                if not addr_to_pair:
                    await asyncio.sleep(60)
                    continue

                # Rug check
                per_chain = defaultdict(list)
                for addr, (_, chain, _, _, _) in addr_to_pair.items():
                    per_chain[chain].append(addr)
                safety = {}
                for chain, addrs in per_chain.items():
                    safety.update(await is_safe_batch(addrs, chain, session))

                alerts = []
                for addr, (p, chain, slug, is_new_pair, pair_addr) in addr_to_pair.items():
                    if not safety.get(addr, False):
                        continue

                    base = p.get("baseToken", {})
                    sym = base.get("symbol", "???")[:20]
                    liq = p.get("liquidity", {}).get("usd", 0) or 0
                    fdv = p.get("fdv", 0) or 0
                    vol = p.get("volume", {}).get("m5", 0) or 0

                    h = vol_hist[addr]
                    h.append(vol)
                    spike = vol / (sum(h) / len(h)) if len(h) > 1 else 1.0
                    volume_spike = spike >= 2.0
                    large_buy = await detect_large_buy(addr, chain)

                    level = get_alert_level(liq, fdv, vol, is_new_pair, volume_spike, large_buy)
                    if not level:
                        continue

                    # Track state
                    state = token_state.get(addr, {"sent_levels": [], "last_vol": 0})
                    sent_before = level in state["sent_levels"]

                    # Resend logic
                    if sent_before:
                        if level == "max" and large_buy:
                            level = "large_buy"
                        elif level == "medium" and "min" in state["sent_levels"]:
                            level = "upgrade"
                        else:
                            continue  # already sent

                    state["sent_levels"].append(level)
                    state["last_vol"] = vol
                    token_state[addr] = state
                    last_alerted[addr] = now
                    seen[addr] = now

                    msg = format_alert(chain, sym, addr, liq, fdv, vol, pair_addr, level)
                    alerts.append((msg, addr, level))

                # SEND
                for msg, addr, level in alerts:
                    sent = 0
                    for uid, u in list(users.items()):
                        is_premium = u.get("paid") and (u.get("paid_until") is None or datetime.fromisoformat(u["paid_until"]) > datetime.utcnow())
                        if u["free"] > 0 or is_premium:
                            if level in ["large_buy", "upgrade"] and not is_premium:
                                continue  # premium only
                            try:
                                await app.bot.send_message(uid, msg, parse_mode="MarkdownV2", disable_web_page_preview=True)
                                if u["free"] > 0 and level not in ["large_buy", "upgrade"]:
                                    u["free"] -= 1
                                sent += 1
                                if sent % 10 == 0:
                                    await asyncio.sleep(0.3)
                            except Exception as exc:
                                log.warning(f"Send to {uid} failed: {exc}")
                    log.info(f"ALERT {level.upper()} → {addr} | Sent to {sent} users")

                await asyncio.sleep(60)

            except Exception as exc:
                log.error(f"SCANNER CRASH: {exc}", exc_info=True)
                await asyncio.sleep(60)

# --------------------------------------------------------------------------- #
#                               MAIN                                        #
# --------------------------------------------------------------------------- #
async def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("owner", owner))

    app.job_queue.run_once(lambda _: None, 0)
    app.create_task(scanner(app))
    app.create_task(auto_save())

    log.info("ONION ALERTS LIVE – Min/Medium/Max + Resends + Premium Only")
    await app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        async with save_lock:
            save_data(data)
        log.info("Shutdown – data saved")
