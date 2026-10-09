import asyncio
import base64
import json
import os
import time
import uuid
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from typing import Dict, List, Optional
import websockets
from dotenv import load_dotenv
from tonsdk.boc import Cell
from tonsdk.contract.wallet import Wallets, WalletVersionEnum
from tonsdk.utils import bytes_to_b64str
import httpx

load_dotenv()

# ===== الإعدادات =====
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
TONCENTER_API_URL = os.getenv("TONCENTER_API_URL", "https://toncenter.com/api/v2")
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "")
OMNISTON_WS_URL = os.getenv("OMNISTON_WS_URL", "wss://omni-ws.ston.fi")
TON_MNEMONIC = os.getenv("TON_MNEMONIC", "").split()
STATE_FILE = "state.json"
CONFIRM_TIMEOUT = 1800
QUOTE_TIMEOUT = 15
TRANSFER_TIMEOUT = 30
BLOCKCHAIN_ID = 607  # TON

# ===== العملات المتداولة =====
TOKENS = {
    "NOT":  {"addr": "EQAvlWFDxGF2lXm67y4yzC17wYKD9A0guwPkMs1gOsM__NOT", "name": "Notcoin"},
    "DOGS": {"addr": "EQCvxJy4eG8hyHBFsZ7eePxrRsUQSFE_jpptRAYBmcG_DOGS", "name": "Dogs"},
    "STON": {"addr": "EQA2kCVNwVsil2EM2mB0SkXytxCqQjS4mttjDpnXmwG9T6bO", "name": "STON.fi"},
}

# ===== الحالة =====
DEFAULT_STATE = {
    "phase": "hunting",
    "current_symbol": None,
    "entry_price": 0.0,
    "target_price": 0.0,
    "trailing_stop": 0.0,
    "highest_price": 0.0,
    "sell_price": 0.0,
    "signal_sent_at": 0,
    "last_update_id": 0,
    "price_history": {},
    "volume_history": {},
    "trades": [],
    "last_daily_report": "",
    "stats": {},
    "recent_signals": [],
    "wallet_address": "",
    "wallet_hex": "",
}

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                s = json.load(f)
                for k in DEFAULT_STATE:
                    if k not in s:
                        s[k] = DEFAULT_STATE[k]
                return s
        except Exception:
            pass
    return dict(DEFAULT_STATE)

def save_state(s):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(s, f, indent=2)
    except Exception as e:
        print(f"save fail: {e}")

async def send(text):
    if not BOT_TOKEN or not CHAT_ID:
        print(text)
        return
    try:
        async with httpx.AsyncClient() as c:
            await c.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
    except Exception as e:
        print(f"send fail: {e}")

async def get_updates(offset):
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 0},
                timeout=15,
            )
            return r.json().get("result", [])
    except Exception:
        return []

async def get_price(addr):
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"https://api.dexscreener.com/latest/dex/search?q={addr}",
                timeout=10,
            )
            data = r.json()
            if "pairs" in data and data["pairs"]:
                pairs = sorted(
                    data["pairs"],
                    key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0,
                    reverse=True,
                )
                p = pairs[0]
                return {
                    "price": float(p.get("priceUsd", 0) or 0),
                    "volume": float(p.get("volume", {}).get("h24", 0) or 0),
                    "liquidity": float(p.get("liquidity", {}).get("usd", 0) or 0),
                    "change_1h": float(p.get("priceChange", {}).get("h1", 0) or 0),
                    "change_24h": float(p.get("priceChange", {}).get("h24", 0) or 0),
                }
    except Exception:
        pass
    return None

# ===== المؤشرات الفنية =====
def ema(d, p):
    if len(d) < p:
        return d[-1] if d else 0
    k = 2 / (p + 1)
    e = sum(d[:p]) / p
    for x in d[p:]:
        e = x * k + e * (1 - k)
    return e

def rsi(d, p=14):
    if len(d) < p + 1:
        return 50
    g, l = [], []
    for i in range(1, len(d)):
        x = d[i] - d[i - 1]
        g.append(max(x, 0))
        l.append(max(-x, 0))
    ag = sum(g[-p:]) / p
    al = sum(l[-p:]) / p
    if al == 0:
        return 100
    return 100 - 100 / (1 + ag / al)

def macd_calc(d):
    if len(d) < 26:
        return {"macd": 0, "signal": 0, "hist": 0}
    e12 = ema(d, 12)
    e26 = ema(d, 26)
    m = e12 - e26
    series = [ema(d[:i], 12) - ema(d[:i], 26) for i in range(26, len(d) + 1)]
    s = ema(series, 9) if len(series) >= 9 else m
    return {"macd": m, "signal": s, "hist": m - s}

def bollinger(d, p=20):
    if len(d) < p:
        return {"pos": 0.5, "width": 0}
    r = d[-p:]
    m = sum(r) / p
    sd = (sum((x - m) ** 2 for x in r) / p) ** 0.5
    if sd == 0:
        return {"pos": 0.5, "width": 0}
    up = m + 2 * sd
    lo = m - 2 * sd
    if up == lo:
        return {"pos": 0.5, "width": 0}
    return {"pos": (d[-1] - lo) / (up - lo), "width": (up - lo) / m if m > 0 else 0}

def stoch(d, p=14):
    if len(d) < p:
        return 50
    r = d[-p:]
    h, l = max(r), min(r)
    if h == l:
        return 50
    return 100 * (d[-1] - l) / (h - l)

def atr_calc(d, p=14):
    if len(d) < p + 1:
        return 0
    trs = [abs(d[i] - d[i - 1]) for i in range(1, len(d))]
    return sum(trs[-p:]) / p

def adx_calc(d, p=14):
    if len(d) < p + 1:
        return 0
    pdm, mdm, tr = [], [], []
    for i in range(1, len(d)):
        up = d[i] - d[i - 1]
        dn = d[i - 1] - d[i]
        pdm.append(up if up > dn and up > 0 else 0)
        mdm.append(dn if dn > up and dn > 0 else 0)
        tr.append(abs(d[i] - d[i - 1]))
    a = sum(tr[-p:]) / p if tr else 1
    if a == 0:
        return 0
    pdi = 100 * (sum(pdm[-p:]) / p) / a
    mdi = 100 * (sum(mdm[-p:]) / p) / a
    if pdi + mdi == 0:
        return 0
    return 100 * abs(pdi - mdi) / (pdi + mdi)

def vwap_calc(prices, volumes):
    if not prices or not volumes or len(prices) != len(volumes):
        return prices[-1] if prices else 0
    tv = sum(volumes)
    if tv == 0:
        return prices[-1]
    return sum(p * v for p, v in zip(prices, volumes)) / tv

def fibonacci_levels(prices):
    if len(prices) < 30:
        return {}
    hi = max(prices[-30:])
    lo = min(prices[-30:])
    diff = hi - lo
    if diff == 0:
        return {}
    return {
        "23.6": hi - 0.236 * diff,
        "38.2": hi - 0.382 * diff,
        "50.0": hi - 0.500 * diff,
        "61.8": hi - 0.618 * diff,
        "78.6": hi - 0.786 * diff,
    }

def whale_score(volumes):
    if len(volumes) < 6:
        return 0, []
    avg = sum(volumes[-6:-1]) / 5
    if avg == 0:
        return 0, []
    ratio = volumes[-1] / avg
    if ratio > 3:
        return 25, [f"🐋 حوت اشترى (حجم x{ratio:.1f})"]
    elif ratio > 2:
        return 15, [f"🐋 حجم مرتفع (x{ratio:.1f})"]
    elif ratio > 1.5:
        return 8, [f"حجم جيد (x{ratio:.1f})"]
    return 0, []

def multi_timeframe(prices):
    if len(prices) < 50:
        return 0, []
    short_trend = ema(prices[-10:], 5) > ema(prices[-10:], 8)
    med_trend = ema(prices[-30:], 10) > ema(prices[-30:], 20)
    long_trend = ema(prices, 20) > ema(prices, 40) if len(prices) >= 40 else med_trend
    score = 0
    reasons = []
    if short_trend and med_trend and long_trend:
        score = 20
        reasons.append("✅ 3 أطر زمنية متوافقة")
    elif short_trend and med_trend:
        score = 12
        reasons.append("✅ إطارين متوافقين")
    elif short_trend:
        score = 5
    return score, reasons

def fib_score(prices):
    levels = fibonacci_levels(prices)
    if not levels:
        return 0, []
    cur = prices[-1]
    for name, lvl in [("61.8", levels.get("61.8")), ("50.0", levels.get("50.0")), ("78.6", levels.get("78.6"))]:
        if lvl and abs(cur - lvl) / lvl < 0.02:
            return 15, [f"📐 مستوى فيبوناتشي {name}%"]
    return 0, []

def vwap_score(prices, volumes):
    if not prices or not volumes:
        return 0, []
    v = vwap_calc(prices, volumes)
    if v == 0:
        return 0, []
    if prices[-1] < v * 0.95:
        return 12, ["💰 السعر تحت VWAP"]
    elif prices[-1] < v:
        return 6, ["💰 السعر قريب من VWAP"]
    return 0, []

def detect_regime(prices):
    if len(prices) < 30:
        return "unknown"
    a = adx_calc(prices)
    at = atr_calc(prices)
    vol = at / prices[-1] if prices[-1] > 0 else 0
    e9 = ema(prices, 9)
    e21 = ema(prices, 21)
    if vol > 0.08:
        return "volatile"
    if a > 25 and e9 > e21:
        return "bullish"
    if a > 25 and e9 < e21:
        return "bearish"
    if a < 20:
        return "ranging"
    return "neutral"

def weighted_score(prices, volumes, regime, state):
    if len(prices) < 20:
        return 50, ["بيانات غير كافية"], {}
    reasons = []
    details = {}

    r = rsi(prices)
    if r < 30:
        rsi_s = 100
        reasons.append(f"RSI ذروة بيع ({r:.0f})")
    elif r < 40:
        rsi_s = 75
        reasons.append(f"RSI منخفض ({r:.0f})")
    elif r > 70:
        rsi_s = 15
    else:
        rsi_s = 50
    details["rsi"] = rsi_s

    m = macd_calc(prices)
    if m["hist"] > 0 and m["macd"] > m["signal"]:
        macd_s = 100
        reasons.append("MACD صاعد قوي")
    elif m["hist"] > 0:
        macd_s = 70
    elif m["hist"] < 0:
        macd_s = 20
    else:
        macd_s = 50
    details["macd"] = macd_s

    wh_s, wh_r = whale_score(volumes)
    if wh_s > 0:
        reasons.extend(wh_r)
    details["whale"] = wh_s * 4

    mt_s, mt_r = multi_timeframe(prices)
    if mt_s > 0:
        reasons.extend(mt_r)
    details["mtf"] = mt_s * 5

    fb_s, fb_r = fib_score(prices)
    if fb_s > 0:
        reasons.extend(fb_r)
    details["fib"] = fb_s * 6

    vw_s, vw_r = vwap_score(prices, volumes)
    if vw_s > 0:
        reasons.extend(vw_r)
    details["vwap"] = vw_s * 8

    b = bollinger(prices)
    if b["pos"] < 0.15:
        b_s = 100
        reasons.append("قاع بولينجر")
    elif b["pos"] < 0.3:
        b_s = 70
    elif b["pos"] > 0.85:
        b_s = 20
    else:
        b_s = 50
    details["boll"] = b_s

    e9 = ema(prices, 9)
    e21 = ema(prices, 21)
    e50 = ema(prices, 50) if len(prices) >= 50 else e21
    if e9 > e21 > e50:
        ema_s = 100
        reasons.append("EMA ترتيب صاعد")
    elif e9 > e21:
        ema_s = 70
    else:
        ema_s = 25
    details["ema"] = ema_s

    s = stoch(prices)
    if s < 20:
        st_s = 100
        reasons.append("ستوكاستك منخفض")
    elif s > 80:
        st_s = 20
    else:
        st_s = 50
    details["stoch"] = st_s

    weights = {
        "rsi": 0.20, "macd": 0.15, "whale": 0.15, "mtf": 0.10,
        "fib": 0.05, "vwap": 0.10, "boll": 0.10, "ema": 0.08, "stoch": 0.07,
    }

    final = 0
    total_w = 0
    for k, w in weights.items():
        if k in details:
            final += details[k] * w
            total_w += w
    if total_w > 0:
        final = final / total_w
    else:
        final = 50

    if regime == "volatile":
        final *= 0.5
        reasons.append("⚠️ سوق متقلب - تخفيض النقاط")
    elif regime == "bearish":
        final *= 0.85

    return min(100, final), reasons, details

# ===== محفظة TON =====
def load_wallet():
    if not TON_MNEMONIC or len(TON_MNEMONIC) < 12:
        return None, None, None
    try:
        version = WalletVersionEnum.v4r2
        mnemonics, pub_k, priv_k, wallet = Wallets.from_mnemonics(
            TON_MNEMONIC, version, workchain=0
        )
        addr_hex = wallet.address.to_string(is_user_friendly=False)
        addr_bounceable = wallet.address.to_string(is_user_friendly=True, is_bounceable=True)
        return wallet, addr_hex, addr_bounceable
    except Exception as e:
        print(f"Wallet load error: {e}")
        return None, None, None

# ===== Omniston: طلب عرض سعر =====
async def request_quote(config, wallet_hex):
    bid_units = str(int(Decimal(config["amount"]) * Decimal(10) ** config["from_token_decimals"]))
    request_id = str(uuid.uuid4())
    params = {
        "bid_asset_address": {"blockchain": BLOCKCHAIN_ID, "address": config["from_token_address"]},
        "ask_asset_address": {"blockchain": BLOCKCHAIN_ID, "address": config["to_token_address"]},
        "amount": {"bid_units": bid_units},
        "referrer_fee_bps": 0,
        "settlement_methods": [0],
        "settlement_params": {
            "max_price_slippage_bps": config["max_slippage_bps"],
            "max_outgoing_messages": config["max_outgoing_messages"],
            "gasless_settlement": 1,
            "flexible_referrer_fee": False,
            "wallet_address": {"blockchain": BLOCKCHAIN_ID, "address": wallet_hex},
        },
    }
    payload = {"jsonrpc": "2.0", "id": request_id, "method": "v1beta7.quote", "params": params}
    try:
        async with websockets.connect(OMNISTON_WS_URL, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps(payload))
            deadline = time.time() + QUOTE_TIMEOUT
            while time.time() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time()))
                data = json.loads(raw)
                if data.get("error"):
                    return None
                quote = _extract_quote(data.get("result")) or _extract_quote(data)
                if quote:
                    return quote
    except Exception:
        pass
    return None

def _extract_quote(data):
    if not isinstance(data, dict):
        return None
    if "bid_units" in data and "ask_units" in data:
        return data
    if "quote" in data and isinstance(data["quote"], dict):
        return data["quote"]
    for value in data.values():
        if isinstance(value, dict):
            found = _extract_quote(value)
            if found:
                return found
    return None

# ===== Omniston: بناء المعاملة =====
async def build_transfer(quote, wallet_hex):
    request_id = str(uuid.uuid4())
    params = {
        "quote": quote,
        "source_address": {"blockchain": BLOCKCHAIN_ID, "address": wallet_hex},
        "destination_address": {"blockchain": BLOCKCHAIN_ID, "address": wallet_hex},
        "gas_excess_address": {"blockchain": BLOCKCHAIN_ID, "address": wallet_hex},
        "use_recommended_slippage": True,
    }
    payload = {"jsonrpc": "2.0", "id": request_id, "method": "v1beta7.transaction.build_transfer", "params": params}
    try:
        async with websockets.connect(OMNISTON_WS_URL, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps(payload))
            deadline = time.time() + TRANSFER_TIMEOUT
            while time.time() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time()))
                data = json.loads(raw)
                if data.get("error"):
                    return None
                transfer = _extract_transfer(data.get("result")) or _extract_transfer(data)
                if transfer:
                    return transfer
    except Exception:
        pass
    return None

def _extract_transfer(data):
    if not isinstance(data, dict):
        return None
    if "ton" in data and isinstance(data["ton"], dict):
        return data
    if "transaction" in data and isinstance(data["transaction"], dict):
        return data["transaction"]
    for value in data.values():
        if isinstance(value, dict):
            found = _extract_transfer(value)
            if found:
                return found
    return None

def _decode_cell(raw):
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        data = base64.b64decode(raw)
    except Exception:
        try:
            data = bytes.fromhex(raw)
        except ValueError:
            return None
    try:
        return Cell.one_from_boc(data)
    except Exception:
        return None

async def execute_swap(quote, wallet_hex, wallet):
    transfer = await build_transfer(quote, wallet_hex)
    if not transfer:
        return False
    ton_section = transfer.get("ton") if isinstance(transfer, dict) else None
    messages = ton_section.get("messages") if isinstance(ton_section, dict) else None
    if not isinstance(messages, list) or not messages:
        return False
    msg = messages[0]
    payload_cell = _decode_cell(msg.get("payload") or msg.get("message_boc") or msg.get("boc"))
    state_init_cell = _decode_cell(msg.get("state_init") or msg.get("jetton_wallet_state_init"))
    seqno = await fetch_seqno(wallet_hex)
    if seqno is None:
        return False
    target_address = msg.get("target_address") or msg.get("address")
    amount_field = msg.get("send_amount") or msg.get("amount")
    try:
        amount_value = int(str(amount_field))
    except Exception:
        return False
    external = wallet.create_transfer_message(
        target_address, amount_value, seqno, payload=payload_cell, state_init=state_init_cell,
    )
    boc = base64.b64encode(external["message"].to_boc(False)).decode("ascii")
    return await send_boc(boc)

async def fetch_seqno(addr_hex):
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{TONCENTER_API_URL}/runGetMethod",
                params={"address": addr_hex, "method": "seqno"},
                timeout=10,
            )
            data = r.json()
            if data.get("ok") and data.get("result"):
                return int(data["result"]["stack"][0][1], 16)
    except Exception:
        pass
    return None

async def send_boc(boc_b64):
    if not TONCENTER_API_KEY:
        return False
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{TONCENTER_API_URL}/sendBoc",
                headers={"X-API-Key": TONCENTER_API_KEY},
                json={"boc": boc_b64},
                timeout=15,
            )
            return r.json().get("ok", False)
    except Exception:
        return False

# ===== مسح السوق =====
async def scan(state):
    best = None
    for sym, info in TOKENS.items():
        md = await get_price(info["addr"])
        if not md or md["price"] == 0:
            continue
        if sym not in state["price_history"]:
            state["price_history"][sym] = []
            state["volume_history"][sym] = []
        state["price_history"][sym].append({"t": time.time(), "price": md["price"]})
        state["price_history"][sym] = state["price_history"][sym][-100:]
        state["volume_history"][sym].append(md["volume"])
        state["volume_history"][sym] = state["volume_history"][sym][-100:]

        prices = [h["price"] for h in state["price_history"][sym]]
        volumes = state["volume_history"][sym]
        regime = detect_regime(prices)
        sc, reasons, _ = weighted_score(prices, volumes, regime, state)

        if md["liquidity"] < 5000:
            sc = 0
            reasons.append("❌ سيولة ضعيفة")

        if best is None or sc > best["score"]:
            best = {
                "symbol": sym,
                "name": info["name"],
                "score": sc,
                "price": md["price"],
                "reasons": reasons,
                "liquidity": md["liquidity"],
                "regime": regime,
            }
    return best

async def check_position(state):
    sym = state["current_symbol"]
    if not sym or sym not in TOKENS:
        return None
    addr = TOKENS[sym]["addr"]
    md = await get_price(addr)
    if not md:
        return None
    pnl = (md["price"] - state["entry_price"]) / state["entry_price"] if state["entry_price"] > 0 else 0
    return {"symbol": sym, "price": md["price"], "pnl": pnl}

# ===== معالجة الرسائل =====
async def handle_messages(state):
    offset = state.get("last_update_id", 0) + 1
    updates = await get_updates(offset)
    for u in updates:
        state["last_update_id"] = u["update_id"]
        text = (u.get("message", {}).get("text") or "").strip().lower()
        if not text:
            continue

        if state["phase"] == "waiting_confirmation":
            if any(k in text for k in ["اشتريت", "شريت", "تم الشراء", "bought"]):
                md = await get_price(TOKENS[state["current_symbol"]]["addr"])
                state["entry_price"] = md["price"] if md else 0
                state["target_price"] = state["entry_price"] * 1.30
                state["trailing_stop"] = state["entry_price"] * 0.92
                state["highest_price"] = state["entry_price"]
                state["phase"] = "holding"
                await send(
                    f"✅ <b>تم تسجيل الشراء</b>\n\n"
                    f"🪙 {state['current_symbol']}\n"
                    f"💰 الدخول: ${state['entry_price']:.6f}\n"
                    f"🎯 الهدف: +30%\n"
                    f"🛑 الوقف المتحرك: -8%\n\n"
                    f"📡 البوت يراقب ويحمي الأرباح..."
                )
            elif any(k in text for k in ["الغاء", "تخطى", "skip"]):
                state["phase"] = "hunting"
                state["current_symbol"] = None
                await send("👌 تم الإلغاء. البوت يرجع يبحث.")

        elif state["phase"] == "waiting_sell_confirmation":
            if any(k in text for k in ["تم البيع", "بعت", "بعته", "sold"]):
                pnl = (state.get("sell_price", 0) - state["entry_price"]) / state["entry_price"] if state["entry_price"] > 0 else 0
                state["trades"].append({
                    "symbol": state["current_symbol"],
                    "entry": state["entry_price"],
                    "exit": state.get("sell_price", 0),
                    "pnl": pnl,
                    "at": time.time(),
                })
                sym = state["current_symbol"]
                if sym not in state["stats"]:
                    state["stats"][sym] = {"wins": 0, "losses": 0}
                if pnl > 0:
                    state["stats"][sym]["wins"] += 1
                else:
                    state["stats"][sym]["losses"] += 1
                state["phase"] = "hunting"
                state["current_symbol"] = None
                state["entry_price"] = 0
                await send(
                    f"✅ <b>تم تسجيل البيع</b>\n"
                    f"💰 الربح: {pnl*100:+.1f}%\n\n"
                    f"🔍 البوت يرجع يبحث عن فرصة جديدة..."
                )
            elif any(k in text for k in ["استمر", "hold"]):
                state["phase"] = "holding"
                await send("👌 تم التأجيل. البوت يكمل مراقبة.")

        elif state["phase"] == "holding" and any(k in text for k in ["حالة", "status"]):
            pos = await check_position(state)
            if pos:
                await send(
                    f"📊 <b>الوضع الحالي</b>\n\n"
                    f"🪙 {pos['symbol']}\n"
                    f"💰 ${pos['price']:.6f}\n"
                    f"📈 {pos['pnl']*100:+.1f}%\n"
                    f"🛑 الوقف: ${state['trailing_stop']:.6f}"
                )

        elif state["phase"] == "hunting" and any(k in text for k in ["حالة", "status"]):
            await send(f"🔍 <b>البوت يبحث عن فرصة قوية</b>\n\n📊 صفقات مكتملة: {len(state['trades'])}")

    return state

async def daily_report(state):
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if state.get("last_daily_report") == today:
        return
    if now.hour < 20:
        return
    state["last_daily_report"] = today
    trades = state["trades"]
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] <= 0)
    pnl = sum(t["pnl"] for t in trades)
    await send(
        f"📊 <b>التقرير اليومي</b>\n"
        f"📅 {today}\n\n"
        f"🏆 صفقات رابحة: {wins}\n"
        f"🔻 صفقات خاسرة: {losses}\n"
        f"💵 صافي PnL: {pnl*100:+.1f}%\n\n"
        f"🔍 الحالة: {state['phase']}"
    )

# ===== الدورة الرئيسية =====
async def run_cycle():
    state = load_state()
    wallet, wallet_hex, wallet_bounceable = load_wallet()
    if not wallet:
        await send("❌ فشل تحميل المحفظة. تأكد من TON_MNEMONIC.")
        return

    state["wallet_address"] = wallet_bounceable
    state["wallet_hex"] = wallet_hex
    state = await handle_messages(state)

    if state["phase"] == "hunting":
        best = await scan(state)
        if best and best["score"] >= 65:
            state["phase"] = "waiting_confirmation"
            state["current_symbol"] = best["symbol"]
            state["signal_sent_at"] = time.time()
            state["recent_signals"].append({"symbol": best["symbol"], "t": time.time()})
            state["recent_signals"] = state["recent_signals"][-10:]

            regime_ar = {
                "bullish": "📈 صاعد", "bearish": "📉 هابط",
                "ranging": "↔️ عرضي", "volatile": "⚡ متقلب",
                "neutral": "⚖️ محايد", "unknown": "❓",
            }.get(best["regime"], "❓")

            reasons_txt = "\n".join(f"• {r}" for r in best["reasons"][:6])

            await send(
                f"🚨 <b>إشارة قوية!</b>\n\n"
                f"🪙 <b>{best['name']} ({best['symbol']})</b>\n"
                f"💰 السعر: ${best['price']:.6f}\n"
                f"📊 النقاط: {best['score']:.0f}/100\n"
                f"📉 نظام السوق: {regime_ar}\n"
                f"💧 السيولة: ${best['liquidity']:.0f}\n\n"
                f"📝 <b>الأسباب:</b>\n{reasons_txt}\n\n"
                f"🟢 <b>جاري التنفيذ التلقائي...</b>"
            )

            # ===== التنفيذ التلقائي =====
            config = {
                "from_token_address": "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c",
                "from_token_decimals": 9,
                "to_token_address": TOKENS[best["symbol"]]["addr"],
                "to_token_decimals": 9,
                "amount": "0.5",
                "max_slippage_bps": 500,
                "max_outgoing_messages": 4,
            }
            quote = await request_quote(config, wallet_hex)
            if quote:
                ok = await execute_swap(quote, wallet_hex, wallet)
                if ok:
                    await send(f"✅ <b>تم التنفيذ التلقائي</b>\n🪙 {best['symbol']}\n💰 السعر: ${best['price']:.6f}")
                    state["entry_price"] = best["price"]
                    state["target_price"] = best["price"] * 1.30
                    state["trailing_stop"] = best["price"] * 0.92
                    state["highest_price"] = best["price"]
                    state["phase"] = "holding"
                else:
                    await send("❌ فشل التنفيذ التلقائي. راجع السجلات.")
                    state["phase"] = "hunting"
            else:
                await send("❌ فشل الحصول على عرض سعر من Omniston.")
                state["phase"] = "hunting"
        else:
            if best:
                print(f"no signal (best {best['symbol']}: {best['score']:.0f})")
            else:
                print("no data")

    elif state["phase"] == "waiting_confirmation":
        if time.time() - state["signal_sent_at"] > CONFIRM_TIMEOUT:
            await send("⏰ ما وصلني تأكيد. ألغي وأرجع أبحث.")
            state["phase"] = "hunting"
            state["current_symbol"] = None

    elif state["phase"] == "holding":
        pos = await check_position(state)
        if pos:
            if pos["price"] > state["highest_price"]:
                state["highest_price"] = pos["price"]
                new_stop = pos["price"] * 0.92
                if new_stop > state["trailing_stop"]:
                    state["trailing_stop"] = new_stop
                    await send(
                        f"📈 <b>رفع الوقف</b>\n"
                        f"🪙 {pos['symbol']}\n"
                        f"السعر: ${pos['price']:.6f}\n"
                        f"الوقف الجديد: ${state['trailing_stop']:.6f}"
                    )

            if pos["price"] >= state["target_price"] or pos["price"] <= state["trailing_stop"]:
                if pos["price"] >= state["target_price"]:
                    reason = "🎯 وصل الهدف (+30%)"
                else:
                    reason = "🛑 ضرب الوقف المتحرك"
                state["phase"] = "waiting_sell_confirmation"
                state["sell_price"] = pos["price"]
                await send(
                    f"🔴 <b>وقت البيع!</b>\n\n"
                    f"🪙 {pos['symbol']}\n"
                    f"💰 ${pos['price']:.6f}\n"
                    f"📈 {pos['pnl']*100:+.1f}%\n"
                    f"📝 {reason}\n\n"
                    f"✍️ لما تبيع اكتب: <b>تم البيع</b>"
                )
            else:
                print(f"holding {pos['symbol']} {pos['pnl']*100:+.1f}%")

    await daily_report(state)
    save_state(state)
