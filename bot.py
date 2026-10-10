import asyncio
import base64
import json
import os
import time
import uuid
from datetime import datetime
from dotenv import load_dotenv
from tonsdk.contract.wallet import Wallets, WalletVersionEnum
import httpx
import websockets

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
TONCENTER_API_URL = "https://toncenter.com/api/v2"
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "")
OMNISTON_WS_URL = "wss://omni-ws.ston.fi"
TON_MNEMONIC = os.getenv("TON_MNEMONIC", "").split()
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"

STATE_FILE = "state.json"
BLOCKCHAIN_ID = 607
CYCLE_SLEEP = 60

TOKENS = {
    "NOT":  {"addr": "EQAvlWFDxGF2lXm67y4yzC17wYKD9A0guwPkMs1gOsM__NOT", "name": "Notcoin"},
    "DOGS": {"addr": "EQCvxJy4eG8hyHBFsZ7eePxrRsUQSFE_jpptRAYBmcG_DOGS", "name": "Dogs"},
    "STON": {"addr": "EQA2kCVNwVsil2EM2mB0SkXytxCqQjS4mttjDpnXmwG9T6bO", "name": "STON.fi"},
}

DEFAULT_STATE = {
    "phase": "hunting",
    "current_symbol": None,
    "last_update_id": 0,
    "last_update_time": 0,
    "price_history": {},
    "volume_history": {},
    "trades": [],
    "last_quote": None,
    "omni_test_result": "",
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
    max_len = 4000
    chunks = [text[i:i+max_len] for i in range(0, len(text), max_len)]
    for chunk in chunks:
        try:
            async with httpx.AsyncClient() as c:
                await c.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    data={"chat_id": CHAT_ID, "text": chunk, "parse_mode": "HTML"},
                    timeout=10,
                )
            await asyncio.sleep(0.3)
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
                }
    except Exception:
        pass
    return None

# ═══════════════════════════════════════
#   المؤشرات
# ═══════════════════════════════════════
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

def weighted_score(prices):
    if len(prices) < 20:
        return 50, ["بيانات غير كافية"]
    reasons = []
    ws, total = 0, 0
    r = rsi(prices)
    if r < 30:
        s = 100; reasons.append(f"RSI={r:.0f}")
    elif r < 40:
        s = 75
    elif r > 70:
        s = 15
    else:
        s = 50
    ws += s * 30; total += 30
    m = macd_calc(prices)
    if m["hist"] > 0:
        s = 100; reasons.append("MACD+")
    else:
        s = 30
    ws += s * 30; total += 30
    e9 = ema(prices, 9)
    e21 = ema(prices, 21)
    if e9 > e21:
        s = 100; reasons.append("EMA+")
    else:
        s = 30
    ws += s * 40; total += 40
    return min(100, ws / total if total > 0 else 50), reasons

async def scan(state):
    best = None
    for sym, info in TOKENS.items():
        md = await get_price(info["addr"])
        if not md or md["price"] == 0:
            continue
        if sym not in state["price_history"]:
            state["price_history"][sym] = []
        state["price_history"][sym].append({"t": time.time(), "price": md["price"]})
        state["price_history"][sym] = state["price_history"][sym][-100:]
        prices = [h["price"] for h in state["price_history"][sym]]
        sc, reasons = weighted_score(prices)
        if md["liquidity"] < 5000:
            sc = 0
            reasons.append("سيولة ضعيفة")
        if best is None or sc > best["score"]:
            best = {"symbol": sym, "name": info["name"], "score": sc,
                    "price": md["price"], "reasons": reasons, "liquidity": md["liquidity"]}
    return best

# ═══════════════════════════════════════
#   Omniston: 1) Quote  2) Build
# ═══════════════════════════════════════
async def get_quote_via_events():
    try:
        async with websockets.connect(OMNISTON_WS_URL, ping_interval=20, ping_timeout=20) as ws:
            req_id = str(uuid.uuid4())
            payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "v1beta7.quote",
                "params": {
                    "bid_asset_address": {"blockchain": BLOCKCHAIN_ID,
                        "address": "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c"},
                    "ask_asset_address": {"blockchain": BLOCKCHAIN_ID,
                        "address": TOKENS["NOT"]["addr"]},
                    "amount": {"bid_units": "100000000"},
                    "referrer_fee_bps": 0,
                    "settlement_methods": [0],
                },
            }
            await ws.send(json.dumps(payload))
            quote_event = None
            all_raw = []
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.5, deadline - time.time()))
                    data = json.loads(raw)
                    all_raw.append(raw)
                    if isinstance(data, dict):
                        res = data.get("result")
                        if isinstance(res, int):
                            continue
                        if isinstance(res, dict):
                            return {"quote": res, "raw": all_raw}
                        if data.get("method") == "event":
                            event = data.get("params", {}).get("result", {}).get("event", {})
                            if "quote_updated" in event:
                                quote_event = event["quote_updated"]
                                break
                except asyncio.TimeoutError:
                    break
            if quote_event:
                return {"quote": quote_event, "raw": all_raw}
            return {"error": "no quote_updated", "raw": all_raw}
    except Exception as e:
        return {"error": f"exception: {str(e)[:150]}"}

async def build_transfer(quote):
    """يبني المعاملة من quote. لا يوقّع ولا يرسل."""
    try:
        if not TON_MNEMONIC or len(TON_MNEMONIC) < 12:
            return {"error": "no mnemonic"}
        _, _, _, wallet = Wallets.from_mnemonics(TON_MNEMONIC, WalletVersionEnum.v4r2, workchain=0)
        wallet_hex = wallet.address.to_string(is_user_friendly=False)

        async with websockets.connect(OMNISTON_WS_URL, ping_interval=20, ping_timeout=20) as ws:
            req_id = str(uuid.uuid4())
            payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "v1beta7.transaction.build_transfer",
                "params": {
                    "quote": quote,
                    "source_address": {"blockchain": BLOCKCHAIN_ID, "address": wallet_hex},
                    "destination_address": {"blockchain": BLOCKCHAIN_ID, "address": wallet_hex},
                    "gas_excess_address": {"blockchain": BLOCKCHAIN_ID, "address": wallet_hex},
                    "use_recommended_slippage": True,
                },
            }
            await ws.send(json.dumps(payload))
            all_raw = []
            deadline = time.time() + 25
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.5, deadline - time.time()))
                    data = json.loads(raw)
                    all_raw.append(raw)
                    if isinstance(data, dict):
                        if data.get("error"):
                            return {"error": f"build error: {str(data['error'])[:200]}", "raw": all_raw}
                        res = data.get("result")
                        if isinstance(res, dict):
                            return {"tx": res, "raw": all_raw}
                except asyncio.TimeoutError:
                    break
            return {"error": "no build result", "raw": all_raw}
    except Exception as e:
        return {"error": f"exception: {str(e)[:150]}"}

async def test_quote():
    lines = ["🧪 <b>اختبار 1: جلب Quote</b>", ""]
    q = await get_quote_via_events()
    if "error" in q:
        lines.append(f"❌ {q['error'][:200]}")
        return "\n".join(lines), None
    quote = q["quote"]
    ask = quote.get("ask_units", "0")
    try:
        ask_f = int(ask) / 1e9
    except Exception:
        ask_f = 0
    lines.append(f"✅ Quote نجح!")
    lines.append(f"💰 0.1 TON → {ask_f:.4f} NOT")
    lines.append(f"📊 quote_id: {quote.get('quote_id', '?')[:20]}...")
    lines.append("")
    lines.append("🎯 الخطوة الجاية: اكتب <b>جرب البناء</b>")
    return "\n".join(lines), quote

async def test_build(quote):
    lines = ["🧪 <b>اختبار 2: بناء المعاملة</b>", ""]
    if not quote:
        lines.append("❌ ماكو quote محفوظ. اكتب <b>اختبار</b> أول.")
        return "\n".join(lines)
    r = await build_transfer(quote)
    if "error" in r:
        lines.append(f"❌ {r['error'][:200]}")
        raw = r.get("raw", [])
        if raw:
            lines.append("")
            lines.append("📋 <b>آخر رد:</b>")
            for x in raw[-2:]:
                safe = x.replace("<", "&lt;").replace(">", "&gt;")
                if len(safe) > 700:
                    safe = safe[:700] + "..."
                lines.append(f"<code>{safe}</code>")
        return "\n".join(lines)
    tx = r["tx"]
    lines.append("✅ <b>Build نجح!</b>")
    lines.append(f"📊 الحقول: {list(tx.keys())}")
    lines.append("")
    lines.append("🎯 قول لي: <b>هيك تمام</b> عشان نضيف التوقيع والإرسال.")
    return "\n".join(lines)

# ═══════════════════════════════════════
#   الحلقة الرئيسية
# ═══════════════════════════════════════
async def handle_messages(state):
    offset = state.get("last_update_id", 0) + 1
    updates = await get_updates(offset)
    now = time.time()
    for u in updates:
        state["last_update_id"] = u["update_id"]
        msg_date = u.get("message", {}).get("date", 0)
        if now - msg_date > 120:
            continue  # نتجاهل الرسائل الأقدم من دقيقتين
        text = (u.get("message", {}).get("text") or "").strip().lower()
        if not text:
            continue

        if "حالة" in text:
            await send(
                f"📊 <b>الحالة</b>\n"
                f"🔍 الوضع: {state['phase']}\n"
                f"💼 صفقات: {len(state['trades'])}\n"
                f"🧪 آخر اختبار: {state.get('omni_test_result', 'ما تم')[:80]}"
            )
        elif "اختبار" in text and "بناء" not in text:
            await send("🧪 جاري جلب Quote...")
            result, quote = await test_quote()
            state["omni_test_result"] = result[:200]
            if quote:
                state["last_quote"] = quote
            await send(result)
        elif "بناء" in text or "جرب البناء" in text:
            await send("🧪 جاري بناء المعاملة...")
            result = await test_build(state.get("last_quote"))
            await send(result)

    return state

async def run_cycle(state):
    state = await handle_messages(state)

    if state["phase"] == "paused":
        return state

    if state["phase"] == "hunting":
        best = await scan(state)
        if best and best["score"] >= 65:
            reasons_txt = "\n".join(f"• {r}" for r in best["reasons"][:5])
            await send(
                f"🚨 <b>إشارة!</b> 🧪 وضع اختبار\n\n"
                f"🪙 {best['name']} ({best['symbol']})\n"
                f"💰 ${best['price']:.6f}\n"
                f"📊 {best['score']:.0f}/100\n\n{reasons_txt}"
            )

    return state

async def main():
    state = load_state()
    await send(
        "🚀 <b>البوت شغال - وضع اختبار</b>\n\n"
        "✍️ <b>اختبار</b> = جلب quote\n"
        "✍️ <b>جرب البناء</b> = بناء المعاملة (بدون إرسال)\n"
        "✍️ <b>حالة</b> = عرض الحالة"
    )
    print("bot started")

    start = time.time()
    while time.time() - start < 5 * 3600 + 50 * 60:
        try:
            state = await run_cycle(state)
            save_state(state)
        except Exception as e:
            print(f"cycle error: {e}")
        await asyncio.sleep(CYCLE_SLEEP)

    save_state(state)
    print("done")

asyncio.run(main())
