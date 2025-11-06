#!/usr/bin/env python3
import os
import asyncio
import json
import time
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

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
# NEW: Official Moralis Pump.fun Endpoints (2025)

# --------------------------------------------------------------------------- #
#                               SAFE SEND                                    #
# --------------------------------------------------------------------------- #
async def safe_send(app, chat_id, text):
    try:
        await app.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="MarkdownV2",
            disable_web_page_preview=True
        )
    except Exception as e:
        log.warning(f"Send failed (chat {chat_id}): {e}")
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
        except:
            pass

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
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
BSCSCAN_API = "https://api.bscscan.com/api"

# RAILWAY
DATA_FILE = Path("/tmp/data.json")
SAVE_INTERVAL = 30
MORALIS_API_KEY = os.getenv("MORALIS_API_KEY")
if not MORALIS_API_KEY:
    raise RuntimeError("MORALIS_API_KEY is required")
MORALIS_NEW_URL = "https://solana-gateway.moralis.io/token/mainnet/exchange/pumpfun/new"
MORALIS_TRENDING_URL = "https://solana-gateway.moralis.io/token/mainnet/exchange/pumpfun/trending"

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
            users_raw = raw.get("users", {})
            for u in users_raw.values():
                u.setdefault("test_sent", False)
                u.setdefault("chat_id", None)
                u.setdefault("filters", {
                    "levels": ["min", "medium", "max"],
                    "chains": ["SOL", "BSC", "PUMP"],
                    "premium_only": False
                })
            return {
                "tracker": raw.get("tracker", {}),
                "users": users_raw,
                "seen": {str(k): v for k, v in raw.get("seen", {}).items()},
                "last_alerted": {str(k): v for k, v in raw.get("last_alerted", {}).items()},
                "token_state": raw.get("token_state", {}),
            }
        except Exception as e:
            log.error(f"Load error: {e}")
    return {
        "tracker": {},
        "users": {},
        "seen": {},
        "last_alerted": {},
        "token_state": {},
    }

def save_data(data):
    try:
        clean_seen = {str(k): v for k, v in data["seen"].items() if k is not None}
        clean_last = {str(k): v for k, v in data["last_alerted"].items() if k is not None}
        clean_token_state = {str(k): v for k, v in data["token_state"].items() if k is not None}

        DATA_FILE.write_text(json.dumps({
            "tracker": data["tracker"],
            "users": data["users"],
            "seen": clean_seen,
            "last_alerted": clean_last,
            "token_state": clean_token_state,
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

def pump_url(ca):
    return f"https://pump.fun/{ca}"

def format_alert(chain, sym, addr, liq, fdv, vol, pair, level):
    e = {"min":"Min","medium":"Medium","max":"Max","large_buy":"SNIPE","upgrade":"UPGRADED"}.get(level, level.upper())
    
    if chain == "PUMP":
        link = pump_url(addr)
    else:
        pair = pair or addr
        link = dex_url(chain, pair)

    # SAFE SYMBOL
    sym = str(sym) if not isinstance(sym, str) else sym
    sym = sym[:20]
    sym_esc = escape_markdown(sym, version=2)

    # SAFE ADDRESS
    addr = str(addr) if not isinstance(addr, str) else addr
    addr = ''.join(c for c in addr if c.isalnum() or c in "+/=")[:64]
    addr_short = addr[:8] + "..." + addr[-6:] if len(addr) >= 14 else addr
    addr_esc = escape_markdown(addr_short, version=2)

    chain_esc = escape_markdown(chain, version=2)
    level_esc = escape_markdown(e, version=2)

    return (
        f"*{level_esc} ALERT* [{chain_esc}]\n"
        f"`{sym_esc}`\n"
        f"*CA:* `{addr_esc}`\n"
        f"Liq: ${liq:,.0f} \\| FDV: ${fdv:,.0f}\n"
        f"5m Vol: ${vol:,.0f}\n"
        f"[View]({link})"
    )

# --------------------------------------------------------------------------- #
#                               RUG / BUY                                   #
# --------------------------------------------------------------------------- #
async def is_safe_batch(addrs, chain, sess):
    if not addrs:
        return {}
    if chain in ["SOL", "PUMP"]:
        # GoPlus doesn't support Solana/Pump — skip or use alternative
        return {a: True for a in addrs}
    
    chain_id = 56  # BSC only
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
    if chain != "SOL":
        return False
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

def get_alert_level(liq, fdv, vol, new, spike, buy, chain="SOL"):
    if chain == "PUMP":
        if vol >= 200 and fdv >= 5000:
            return "min"
        if liq >= 15000 and fdv >= 40000 and vol >= 1500:
            return "medium"
        return None
    else:
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
        [InlineKeyboardButton(f"{'ON' if 'PUMP' in chains else 'OFF'} Pump.fun", callback_data="toggle_pump")],
        [],
        [InlineKeyboardButton("Save Settings", callback_data="save_settings")]
    ])

# --------------------------------------------------------------------------- #
#                               COMMANDS                                    #
# --------------------------------------------------------------------------- #
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    log.info(f"START from {uid} in chat {chat_id}")

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
            "chat_id": chat_id,
            "filters": {"levels": ["min", "medium", "max"], "chains": ["SOL", "BSC", "PUMP"], "premium_only": False}
        }

    user = users[uid]
    user["chat_id"] = chat_id

    welcome_html = (
        f"<b>ONION ALERTS</b>\n\n"
        f"Free trial: <code>{user['free']}</code> alerts left\n"
        f"Subscribe: <code>${PRICE_USDT}/mo</code>\n\n"
        f"<b>Pay USDT (BSC):</b>\n<code>{WALLETS['BSC']}</code>\n\n"
        f"After payment send TXID:\n<code>/pay YOUR_TXID</code>\n"
        f"<i>Auto-upgrade in less than 2 min!</i>"
    )
    await update.message.reply_text(welcome_html, parse_mode="HTML")

    if not user.get("test_sent", False):
        test = (
            f"*TEST ALERT \\(PUMP\\)*\n"
            f"`TESTCOIN`\n"
            f"*CA:* `test123456789abcdefghi123456789abcdefghi`\n"
            f"Liq: $500 \\| FDV: $8,000\n"
            f"5m Vol: $300\n"
            f"[Pump\\.fun](https://pump\\.fun/test123456789abcdefghi123456789abcdefghi)\n\n"
            f"_This test does **not** use a free trial_"
        )
        await update.message.reply_text(test, parse_mode="MarkdownV2", disable_web_page_preview=True)
        user["test_sent"] = True
        log.info(f"Test alert sent in chat {chat_id}")

async def testalert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    msg = (
        f"*MIN ALERT* \\[PUMP\\]\n"
        f"`FAKE`\n"
        f"*CA:* `fake1234567890`\n"
        f"Liq: $600 \\| FDV: $12,000\n"
        f"5m Vol: $450\n"
        f"[Pump\\.fun](https://pump\\.fun/fake1234567890)"
    )
    await update.message.reply_text(msg, parse_mode="MarkdownV2")
    await update.message.reply_text("Fake alert sent in chat!")

async def force(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("FORCE: BOT IS ALIVE IN THIS CHAT")

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

                    user = users[uid]
                    source = user.get("source", "organic")
                    influencer = source.split("_", 1)[1] if "_" in source else None
                    if influencer:
                        tracker.setdefault(influencer, {"joins": 0, "subs": 0, "revenue": 0.0})
                        tracker[influencer]["subs"] += 1
                        tracker[influencer]["revenue"] += PRICE_USDT

                    await update.message.reply_text("*Payment confirmed!* Premium active.", parse_mode="MarkdownV2")
                    return
    except Exception as e:
        log.error(f"Pay error: {e}")
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
        f"*{escape_markdown(inf.upper(), version=2)}*\nJoins: `{s['joins']}`\nSubs: `{s['subs']}`\nRevenue: `${s['revenue']:.2f}`",
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

async def settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in users:
        users[uid] = {"free": FREE_ALERTS, "chat_id": update.effective_chat.id, "filters": {"levels": ["min","medium","max"], "chains": ["SOL","BSC","PUMP"]}}
    users[uid]["chat_id"] = update.effective_chat.id
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
        if key == "pump":
            lst = f["chains"]
            item = "PUMP"
        else:
            lst = f["levels"] if key in ["min","medium","max"] else f["chains"] if key in ["sol","bsc"] else None
            item = key if key in ["min","medium","max"] else key.upper()
        if lst and item:
            if item in lst:
                lst.remove(item)
            else:
                lst.append(item)
        await query.edit_message_reply_markup(reply_markup=build_settings_kb(f))
    elif data == "save_settings":
        await query.edit_message_text("Settings saved!")
        await query.message.reply_text("Your filters are now active.")

async def pump_scanner(app: Application):
    headers = {
        "accept": "application/json",
        "X-API-Key": MORALIS_API_KEY
    }
    log.info("PUMP SCANNER: Starting (new + volume spikes)")

    volume_history = defaultdict(lambda: deque(maxlen=3))

    async with aiohttp.ClientSession() as sess:
        while True:
            try:
                all_tokens = []
                seen_addrs = set()

                # === NEW TOKENS ===
                try:
                    params = {
                        "chain": "solana",
                        "exchange": "pumpfun",
                        "limit": 20
                    }
                    async with sess.get(
                        "https://solana-gateway.moralis.io/token/mainnet/new-tokens-by-exchange",
                        headers=headers,
                        params=params,
                        timeout=15
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            new_tokens = data.get("result", [])
                            log.info(f"Moralis NEW: {len(new_tokens)} new tokens")
                            for t in new_tokens:
                                addr = t.get("mint") or t.get("tokenAddress") or ""
                                addr = str(addr)[:64]
                                if addr and len(addr) >= 10 and addr not in seen_addrs:
                                    seen_addrs.add(addr)
                                    all_tokens.append(t)
                except Exception as e:
                    log.warning(f"Moralis NEW error: {e}")

                # === TRENDING TOKENS ===
                try:
                    params = {
                        "chain": "solana",
                        "exchange": "pumpfun",
                        "limit": 30,
                        "sort": "volume_5m_desc"
                    }
                    async with sess.get(
                        "https://solana-gateway.moralis.io/token/mainnet/tokens-by-exchange",
                        headers=headers,
                        params=params,
                        timeout=15
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            trending = data.get("result", [])
                            log.info(f"Moralis TRENDING: {len(trending)} trending tokens")
                            for t in trending:
                                addr = t.get("mint") or t.get("tokenAddress") or ""
                                addr = str(addr)[:64]
                                if addr and len(addr) >= 10 and addr not in seen_addrs:
                                    seen_addrs.add(addr)
                                    all_tokens.append(t)
                except Exception as e:
                    log.warning(f"Moralis TRENDING error: {e}")

                if not all_tokens:
                    log.info("Moralis: No tokens this cycle")
                    await asyncio.sleep(10)
                    continue

                # === PROCESS EACH TOKEN ===
                for token in all_tokens:
                    try:
                        addr = token.get("mint") or token.get("tokenAddress") or ""
                        addr = str(addr)[:64]
                        if not addr or len(addr) < 10:
                            continue

                        # Cooldown: 5 min
                        if addr in seen and time.time() - seen[addr] < 300:
                            continue
                        seen[addr] = time.time()

                        sym = str(token.get("symbol", "???"))[:20]
                        vol = float(token.get("volumeUSD", 0) or token.get("volume_5m", 0) or 0)
                        fdv = float(token.get("marketCapUSD", 0) or token.get("fdv", 0) or 0)
                        liq = fdv * 0.1 if fdv > 0 else 0

                        # Volume spike
                        prev_vols = volume_history[addr]
                        prev_vols.append(vol)
                        spike = False
                        if len(prev_vols) > 1:
                            avg_prev = sum(prev_vols[:-1]) / len(prev_vols[:-1])
                            if avg_prev > 0 and vol / avg_prev >= 2.0:
                                spike = True

                        level = get_alert_level(liq, fdv, vol, True, spike, False, "PUMP")
                        if not level:
                            continue

                        # Escalation logic
                        state = token_state.get(addr, {"sent_levels": []})
                        if level in state["sent_levels"]:
                            continue
                        state["sent_levels"].append(level)
                        token_state[addr] = state

                        # Send alert
                        msg = format_alert("PUMP", sym, addr, liq, fdv, vol, None, level)
                        sent = 0
                        for uid, u in list(users.items()):
                            if "chat_id" not in u or not u["chat_id"]:
                                continue
                            chat_id = u["chat_id"]
                            is_premium = u.get("paid") and (
                                u.get("paid_until") is None or
                                datetime.fromisoformat(u["paid_until"]) > datetime.utcnow()
                            )
                            if u["free"] <= 0 and not is_premium:
                                continue
                            f = u.get("filters", {})
                            if level not in f.get("levels", []) or "PUMP" not in f.get("chains", []):
                                continue
                            await safe_send(app, chat_id, msg)
                            if u["free"] > 0:
                                u["free"] -= 1
                            sent += 1
                        log.info(f"PUMP {level.upper()} -> {addr} | Vol: ${vol:,.0f} | Sent to {sent}")

                    except Exception as e:
                        log.error(f"Token error: {e}")

                await asyncio.sleep(10)

            except Exception as e:
                log.error(f"PUMP SCANNER CRASH: {e}")
                await asyncio.sleep(15)

# --------------------------------------------------------------------------- #
#                             DEX SCANNER (LIVE)                              #
# --------------------------------------------------------------------------- #
async def dex_scanner(app: Application):
    async with aiohttp.ClientSession() as sess:
        while True:
            try:
                log.info("DEX SCANNER: Starting Birdeye cycle...")
                candidates = []

                for chain in ["solana", "bsc"]:
                    url = f"https://public-api.birdeye.so/defi/v2.0/new_pairs?chain={chain}"
                    try:
                        async with sess.get(url, timeout=15) as r:
                            if r.status == 200:
                                data = await r.json()
                                pairs = data.get("data", {}).get("pairs", [])[:50]
                                for p in pairs:
                                    addr = p.get("baseToken", {}).get("address")
                                    pair_addr = p.get("pairAddress")
                                    if not addr or not pair_addr or len(pair_addr) < 30:
                                        continue
                                    candidates.append((p, chain.upper(), pair_addr))
                    except Exception as e:
                        log.error(f"BIRDEYE fetch error {chain}: {e}")

                if not candidates:
                    await asyncio.sleep(60)
                    continue

                addr_to_pair = {}
                for p, chain, pair_addr in candidates:
                    addr = p.get("baseToken", {}).get("address")
                    if addr in seen and time.time() - seen[addr] < 300:
                        continue
                    addr_to_pair[addr] = (p, chain, pair_addr)

                per_chain = defaultdict(list)
                for addr, (_, chain, _) in addr_to_pair.items():
                    per_chain[chain].append(addr)
                safety = {}
                for chain, addrs in per_chain.items():
                    safety.update(await is_safe_batch(addrs, chain, sess))

                alerts = []
                for addr, (p, chain, pair_addr) in addr_to_pair.items():
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
                    level = get_alert_level(liq, fdv, vol, True, volume_spike, large_buy, chain)
                    if not level:
                        continue

                    state = token_state.get(addr, {"sent_levels": []})
                    if level in state["sent_levels"]:
                        if level == "max" and large_buy:
                            level = "large_buy"
                        elif level == "medium" and "min" in state["sent_levels"]:
                            level = "upgrade"
                        else:
                            continue

                    state["sent_levels"].append(level)
                    token_state[addr] = state
                    seen[addr] = time.time()

                    msg = format_alert(chain, sym, addr, liq, fdv, vol, pair_addr, level)
                    alerts.append((msg, addr, level, chain))

                for msg, addr, level, chain in alerts:
                    sent = 0
                    for uid, u in list(users.items()):
                        if "chat_id" not in u or not u["chat_id"]:
                            continue
                        chat_id = u["chat_id"]
                        is_premium = u.get("paid") and (u.get("paid_until") is None or datetime.fromisoformat(u["paid_until"]) > datetime.utcnow())
                        if u["free"] <= 0 and not is_premium:
                            continue
                        f = u.get("filters", {"levels": ["min","medium","max"], "chains": ["SOL","BSC","PUMP"]})
                        if level not in f["levels"] or chain not in f["chains"]:
                            continue
                        await safe_send(app, chat_id, msg)
                        if u["free"] > 0 and level not in ["large_buy", "upgrade"]:
                            u["free"] -= 1
                        sent += 1
                    log.info(f"BIRDEYE {level.upper()} → {addr} | Sent to {sent}")

                await asyncio.sleep(60)

            except Exception as e:
                log.error(f"DEX SCANNER CRASH: {e}")
                await asyncio.sleep(60)

# --------------------------------------------------------------------------- #
#                               MAIN                                        #
# --------------------------------------------------------------------------- #
async def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("testalert", testalert))
    app.add_handler(CommandHandler("force", force))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("owner", owner))
    app.add_handler(CommandHandler("reset", reset_user))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CallbackQueryHandler(button))

    app.create_task(dex_scanner(app))
    app.create_task(pump_scanner(app))
    app.create_task(auto_save())

    log.info("BOT STARTED – ALERTS COMING")

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True, timeout=30)

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

if __name__ == "__main__":
    asyncio.run(main())
