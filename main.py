#!/usr/bin/env python3
import os
import asyncio
import json
import time
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from telegram.helpers import escape_markdown

def safe_md(t):
    return escape_markdown(str(t), version=2)

import aiohttp
import requests
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
WALLETS = {"BSC": os.getenv("WALLET_BSC", "0xa11351776d6f483418b73c8e40bc706c93e8b1e1")}

GOPLUS_API = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={addrs}"
NEW_PAIRS_URL = "https://api.dexscreener.com/latest/dex/new-pairs/{chain}"
SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q={chain}"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
BSCSCAN_API = "https://api.bscscan.com/api"

DATA_FILE = Path("data.json")
SAVE_INTERVAL = 30

# --------------------------------------------------------------------------- #
#                                 LOGGING                                   #
# --------------------------------------------------------------------------- #
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("onion")

# --------------------------------------------------------------------------- #
#                               PERSISTENCE                                 #
# --------------------------------------------------------------------------- #
def load_data():
    if DATA_FILE.is_file():
        try:
            raw = json.loads(DATA_FILE.read_text())
            now = time.time()
            seen = {k: v for k, v in raw.get("seen", {}).items() if now - v < 86400}
            last = {k: v for k, v in raw.get("last_alerted", {}).items() if now - v < 3600}
            users_raw = raw.get("users", {})
            for u in users_raw.values():
                u.setdefault("test_sent", False)
                u.setdefault("filters", {
                    "levels": ["min", "medium", "max"],
                    "chains": ["SOL", "BSC"],
                    "premium_only": False
                })
            return {
                "tracker": raw.get("tracker", {}),
                "users": users_raw,
                "seen": seen,
                "last_alerted": last,
                "token_state": raw.get("token_state", {}),
            }
        except Exception as e:
            log.error(f"Load error: {e}")
    return {"tracker": {}, "users": {}, "seen": {}, "last_alerted": {}, "token_state": {}}

def save_data(data):
    try:
        DATA_FILE.write_text(json.dumps({
            "tracker": data["tracker"],
            "users": data["users"],
            "seen": data["seen"],
            "last_alerted": data["last_alerted"],
            "token_state": data["token_state"],
        }, indent=2))
    except Exception as e:
        log.error(f"Save error: {e}")

data = load_data()
tracker = data["tracker"]
users = data["users"]
seen = data["seen"]
last_alerted = data["last_alerted"]
token_state = data["token_state"]

vol_hist = defaultdict(lambda: deque(maxlen=5))
goplus_cache = {}
save_lock = asyncio.Lock()

# --------------------------------------------------------------------------- #
#                               AUTO SAVE                                   #
# --------------------------------------------------------------------------- #
async def auto_save():
    while True:
        await asyncio.sleep(SAVE_INTERVAL)
        async with save_lock:
            save_data(data)

# --------------------------------------------------------------------------- #
#                               HELPERS                                     #
# --------------------------------------------------------------------------- #
def dex_url(chain, pair):
    return f"https://dexscreener.com/{'solana' if chain == 'SOL' else 'bsc'}/{pair}"

def safe_md(t):
    return escape_markdown(str(t), version=2)

def format_alert(chain, sym, addr, liq, fdv, vol, pair, level):
    e = {"min":"Min","medium":"Medium","max":"Max","large_buy":"SNIPE","upgrade":"UPGRADED"}.get(level, level.upper())
    return (
        f"*{e} ALERT* [{safe_md(chain)}]\n"
        f"`{safe_md(sym)}`\n"
        f"*CA:* `{safe_md(addr)}`\n"
        f"Liq: ${liq:,.0f} | FDV: ${fdv:,.0f}\n"
        f"5m Vol: ${vol:,.0f}\n"
        f"[DexScreener]({dex_url(chain, pair)})"
    )

# --------------------------------------------------------------------------- #
#                               RUG / BUY                                   #
# --------------------------------------------------------------------------- #
async def is_safe_batch(addrs, chain, sess):
    if not addrs:
        return {}
    chain_id = 56 if chain == "BSC" else 1
    url = GOPLUS_API.format(chain_id=chain_id, addrs=",".join(addrs))
    now = time.time()
    cached = {a: goplus_cache[a][0] for a in addrs if a in goplus_cache and now - goplus_cache[a][1] < 3600}
    to_check = [a for a in addrs if a not in cached]
    results = cached.copy()
    if not to_check:
        return results
    try:
        async with sess.get(url, timeout=10) as resp:
            if resp.status != 200:
                raise ValueError()
            payload = await resp.json()
            for addr in to_check:
                info = payload.get("result", {}).get(addr.lower(), {})
                safe = (
                    info.get("is_open_source") == "1" and
                    info.get("honeypot") == "0" and
                    info.get("can_take_back_ownership") != "1"
                )
                results[addr] = safe
                goplus_cache[addr] = (safe, now)
    except:
        for addr in to_check:
            results[addr] = False
    return results

async def detect_large_buy(addr, chain):
    if chain == "SOL":
        try:
            payload = {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress", "params": [addr, {"limit": 20}]}
            resp = requests.post(SOLANA_RPC, json=payload, timeout=8).json()
            for sig in resp.get("result", [])[:5]:
                tx = requests.post(SOLANA_RPC, json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTransaction",
                    "params": [sig["signature"], {"encoding": "jsonParsed"}]
                }, timeout=8).json()
                if tx.get("result"):
                    pre, post = tx["result"]["meta"]["preBalances"][0], tx["result"]["meta"]["postBalances"][0]
                    if (pre - post) / 1e9 * 180 > 2000:
                        return True
        except:
            pass
    return False

def get_alert_level(liq, fdv, vol, new, spike, buy):
    if new and liq >= 1000 and fdv >= 10000 and vol >= 500:
        return "min"
    if liq >= 25000 and fdv >= 70000 and vol >= 3000:
        return "medium"
    if buy and spike:
        return "max"
    return None

# --------------------------------------------------------------------------- #
#                               FILTERS                                     #
# --------------------------------------------------------------------------- #
def build_settings_kb(f):
    levels = f.get("levels", [])
    chains = f.get("chains", [])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'ON' if 'min' in levels else 'OFF'} Min ($1k liq)", callback_data="toggle_min")],
        [InlineKeyboardButton(f"{'ON' if 'medium' in levels else 'OFF'} Medium ($25k liq)", callback_data="toggle_medium")],
        [InlineKeyboardButton(f"{'ON' if 'max' in levels else 'OFF'} Max / Snipe", callback_data="toggle_max")],
        [],
        [InlineKeyboardButton(f"{'ON' if 'SOL' in chains else 'OFF'} Solana", callback_data="toggle_sol")],
        [InlineKeyboardButton(f"{'ON' if 'BSC' in chains else 'OFF'} BSC", callback_data="toggle_bsc")],
        [],
        [InlineKeyboardButton("Save Settings", callback_data="save_settings")]
    ])

# --------------------------------------------------------------------------- #
#                               COMMANDS                                    #
# --------------------------------------------------------------------------- #
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    log.info(f"START RECEIVED – User: {uid}, Chat: {chat_id}")

    # 1. Debug message (plain text)
    await update.message.reply_text("START COMMAND RECEIVED!")

    args = ctx.args or []
    source = args[0] if args and args[0].startswith("track_") else "organic"
    influencer = source.split("_", 1)[1] if "_" in source else None
    if influencer:
        tracker.setdefault(influencer, {"joins": 0, "subs": 0, "revenue": 0.0})["joins"] += 1

    if uid not in users:
        users[uid] = {
            "free": FREE_ALERTS,
            "source": source,
            "paid": False,
            "paid_until": None,
            "test_sent": False,
            "filters": {"levels": ["min", "medium", "max"], "chains": ["SOL", "BSC"], "premium_only": False}
        }

    user = users[uid]

    # === 2. WELCOME MESSAGE (FIXED MARKDOWN) ===
    wallet_escaped = safe_md(WALLETS["BSC"])
    welcome = (
        f"*ONION ALERTS*\n\n"
        f"Free trial: `{user['free']}` alerts left\n"
        f"Subscribe: `${PRICE_USDT}/mo`\n\n"
        f"*Pay USDT \\(BSC\\):*\n`{wallet_escaped}`\n\n"
        f"After payment send TXID:\n`/pay YOUR\\_TXID`\n"
        f"_Auto\\-upgrade in less than 2 min!_"
    )
    try:
        await update.message.reply_text(welcome, parse_mode="MarkdownV2")
        log.info("Welcome message sent")
    except Exception as e:
        log.error(f"Welcome failed: {e}")
        await update.message.reply_text("Welcome failed (Markdown error). Check logs.")

    # === 3. TEST ALERT DM (FIXED MARKDOWN) ===
    if not user.get("test_sent", False):
        test = (
            f"*TEST ALERT*\n"
            f"`ONIONCOIN`\n"
            f"*CA:* `onion123456789abcdefghi123456789abcdefghi`\n"
            f"Liq: $9,200 \\| FDV: $52,000\n"
            f"5m Vol: $15,600\n"
            f"[DexScreener](https://dexscreener.com/solana/onion123456789abcdefghi123456789abcdefghi)\n\n"
            f"_This test does **not** use a free trial\\._"
        )
        try:
            await ctx.bot.send_message(uid, test, parse_mode="MarkdownV2", disable_web_page_preview=True)
            user["test_sent"] = True
            log.info(f"Test alert DM sent to {uid}")
        except Exception as e:
            log.warning(f"Test DM failed: {e}")
            await ctx.bot.send_message(uid, "TEST DM FAILED (check bot logs)", disable_notification=True)

# === OTHER HANDLERS (unchanged) ===
async def settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in users:
        users[uid] = {"free": FREE_ALERTS, "filters": {"levels": ["min","medium","max"], "chains": ["SOL","BSC"]}}
    f = users[uid]["filters"]
    await update.message.reply_text(
        "*Your Alert Filters*\n\nCustomize what you receive:",
        reply_markup=build_settings_kb(f),
        parse_mode="MarkdownV2"
    )

async def button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if uid not in users:
        return
    f = users[uid]["filters"]
    data = query.data

    if data.startswith("toggle_"):
        key = data[7:]
        lst = f["levels"] if key in ["min","medium","max"] else f["chains"] if key in ["sol","bsc"] else None
        if lst:
            item = key if key in ["min","medium","max"] else key.upper()
            if item in lst:
                lst.remove(item)
            else:
                lst.append(item)
        await query.edit_message_reply_markup(reply_markup=build_settings_kb(f))
    elif data == "save_settings":
        await query.edit_message_text("Settings saved!")
        await query.message.reply_text("Your filters are now active.")

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
                    await update.message.reply_text("*Payment confirmed!* Premium active.", parse_mode="MarkdownV2")
                    return
    except:
        pass
    await update.message.reply_text("Invalid TXID.")

async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/stats <username>`", parse_mode="MarkdownV2")
        return
    inf = ctx.args[0].lower()
    if inf not in tracker:
        await update.message.reply_text("No data.", parse_mode="MarkdownV2")
        return
    s = tracker[inf]
    await update.message.reply_text(
        f"*{safe_md(inf.upper())}*\nJoins: `{s['joins']}`\nSubs: `{s['subs']}`",
        parse_mode="MarkdownV2"
    )

async def owner(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("Owner panel active.")

async def reset_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    target = update.effective_user.id if not ctx.args else int(ctx.args[0])
    users.pop(target, None)
    await update.message.reply_text(f"Reset user {target}")

# --------------------------------------------------------------------------- #
#                               SCANNER                                     #
# --------------------------------------------------------------------------- #
async def scanner(app: Application):
    async with aiohttp.ClientSession() as sess:
        while True:
            try:
                candidates = []
                for chain, slug in [("SOL","solana"), ("BSC","bsc")]:
                    try:
                        async with sess.get(NEW_PAIRS_URL.format(chain=slug), timeout=10) as r:
                            if r.status == 200:
                                for p in (await r.json()).get("pairs", [])[:50]:
                                    candidates.append((p, chain, slug, "new"))
                    except:
                        pass
                    try:
                        async with sess.get(SEARCH_URL.format(chain=slug), timeout=10) as r:
                            if r.status == 200:
                                for p in (await r.json()).get("pairs", [])[50:150]:
                                    candidates.append((p, chain, slug, "search"))
                    except:
                        pass

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

                per_chain = defaultdict(list)
                for addr, (_, chain, _, _, _) in addr_to_pair.items():
                    per_chain[chain].append(addr)
                safety = {}
                for chain, addrs in per_chain.items():
                    safety.update(await is_safe_batch(addrs, chain, sess))

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

                    state = token_state.get(addr, {"sent_levels": [], "last_vol": 0})
                    if level in state["sent_levels"]:
                        if level == "max" and large_buy:
                            level = "large_buy"
                        elif level == "medium" and "min" in state["sent_levels"]:
                            level = "upgrade"
                        else:
                            continue

                    state["sent_levels"].append(level)
                    state["last_vol"] = vol
                    token_state[addr] = state
                    last_alerted[addr] = time.time()
                    seen[addr] = time.time()

                    msg = format_alert(chain, sym, addr, liq, fdv, vol, pair_addr, level)
                    alerts.append((msg, addr, level, chain))

                for msg, addr, level, chain in alerts:
                    sent = 0
                    for uid, u in list(users.items()):
                        is_premium = u.get("paid") and (u.get("paid_until") is None or datetime.fromisoformat(u["paid_until"]) > datetime.utcnow())
                        if u["free"] <= 0 and not is_premium:
                            continue
                        f = u.get("filters", {"levels": ["min","medium","max"], "chains": ["SOL","BSC"], "premium_only": False})
                        if level not in f["levels"] or chain not in f["chains"]:
                            continue
                        if f.get("premium_only", False) and level not in ["large_buy", "upgrade"]:
                            continue
                        if level in ["large_buy", "upgrade"] and not is_premium:
                            continue
                        try:
                            await app.bot.send_message(uid, msg, parse_mode="MarkdownV2", disable_web_page_preview=True)
                            if u["free"] > 0 and level not in ["large_buy", "upgrade"]:
                                u["free"] -= 1
                            sent += 1
                            if sent % 10 == 0:
                                await asyncio.sleep(0.3)
                        except Exception as e:
                            log.warning(f"Send to {uid} failed: {e}")
                    log.info(f"ALERT {level.upper()} to {addr} | Sent to {sent} users")

                await asyncio.sleep(60)
            except Exception as e:
                log.error(f"Scanner error: {e}")
                await asyncio.sleep(60)

# --------------------------------------------------------------------------- #
#                               MAIN                                        #
# --------------------------------------------------------------------------- #
async def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("owner", owner))
    app.add_handler(CommandHandler("reset", reset_user))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CallbackQueryHandler(button))

    # Start background tasks
    app.create_task(scanner(app))
    app.create_task(auto_save())

    log.info("BOT STARTED – polling for updates...")

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        log.info("Shutting down...")
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        async with save_lock:
            save_data(data)
        log.info("Shutdown complete")

if __name__ == "__main__":
    asyncio.run(main())
