import asyncio
import base64
import json
import os
import time
import uuid
from decimal import Decimal
from datetime import datetime
from dotenv import load_dotenv
from tonsdk.contract.wallet import Wallets, WalletVersionEnum
from tonsdk.boc import Cell
import httpx
import websockets

load_dotenv()

# ═══════════════════════════════════════
#   الإعدادات
# ═══════════════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
TONCENTER_API_URL = "https://toncenter.com/api/v2"
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "")
OMNISTON_WS_URL = "wss://omni-ws.ston.fi"
TON_MNEMONIC = os.getenv("TON_MNEMONIC", "").split()
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"  # 1 = اختبار، 0 = حقيقي

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
    "entry_price": 0.0,
    "target_price": 0.0,
    "trailing_stop": 0.0,
    "highest_price": 0.0,
    "last_update_id": 0,
    "price_history": {},
    "volume_history": {},
    "trades": [],
    "last_daily_report": "",
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

def bollinger(d, p=20):
    if len(d) < p:
        return {"pos": 0.5}
    r = d[-p:]
    m = sum(r) / p
    sd = (sum((x - m) ** 2 for x in r) / p) ** 0.5
    if sd == 0:
        return {"pos": 0.5}
    up = m + 2 * sd
    lo = m - 2 * sd
    if up == lo:
        return {"pos": 0.5}
    return {"pos": (d[-1] - lo) / (up - lo)}

def stoch(d, p=14):
    if len(d) < p:
        return 50
    r = d[-p:]
    h, l = max(r), min(r)
    if h == l:
        return 50
    return 100 * (d[-1] - l) / (h - l)

def detect_regime(prices):
    if len(prices) < 30:
        return "unknown"
    at = sum(abs(prices[i] - prices[i - 1]) for i in range(1, min(15, len(prices)))) / 14
    vol = at / prices[-1] if prices[-1] > 0 else 0
    e9 = ema(prices, 9)
    e21 = ema(prices, 21)
    if vol > 0.08:
        return "volatile"
    if e9 > e21:
        return "bullish"
    if e9 < e21:
        return "bearish"
    return "neutral"

def weighted_score(prices, volumes):
    if len(prices) < 20:
        return 50, ["بيانات غير كافية"]
    reasons = []
    ws, total = 0, 0

    r = rsi(prices)
    if r < 30:
        s = 100; reasons.append(f"RSI ذروة بيع ({r:.0f})")
    elif r < 40:
        s = 75; reasons.append(f"RSI منخفض ({r:.0f})")
    elif r > 70:
        s = 15
    else:
        s = 50
    ws += s * 20; total += 20

    m = macd_calc(prices)
    if m["hist"] > 0 and m["macd"] > m["signal"]:
        s = 100; reasons.append("MACD صاعد")
    elif m["hist"] > 0:
        s = 70
    else:
        s = 20
    ws += s * 15; total += 15

    if len(volumes) >= 6:
        avg = sum(volumes[-6:-1]) / 5
        if avg > 0:
            ratio = volumes[-1] / avg
            if ratio > 2:
                s = 100; reasons.append(f"حجم x{ratio:.1f}")
            elif ratio > 1.5:
                s = 70
            else:
                s = 50
            ws += s * 15; total += 15

    b = bollinger(prices)
    if b["pos"] < 0.15:
        s = 100; reasons.append("قاع بولينجر")
    elif b["pos"] < 0.3:
        s = 70
    elif b["pos"] > 0.85:
        s = 20
    else:
        s = 50
    ws += s * 20; total += 20

    e9 = ema(prices, 9)
    e21 = ema(prices, 21)
    if e9 > e21:
        s = 100; reasons.append("EMA صاعد")
    else:
        s = 30
    ws += s * 15; total += 15

    st = stoch(prices)
    if st < 20:
        s = 100; reasons.append("ستوكاستك منخفض")
    else:
        s = 50
    ws += s * 15; total += 15

    final = ws / total if total > 0 else 50
    return min(100, final), reasons

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
        sc, reasons = weighted_score(prices, volumes)

        if md["liquidity"] < 5000:
            sc = 0
            reasons.append("سيولة ضعيفة")

        if best is None or sc > best["score"]:
            best = {
                "symbol": sym,
                "name": info["name"],
                "score": sc,
                "price": md["price"],
                "reasons": reasons,
                "liquidity": md["liquidity"],
            }
    return best

# ═══════════════════════════════════════
#   Omniston Test
# ═══════════════════════════════════════
async def test_omni_connection():
    """اختبار الاتصال بـ Omniston بدون أي معاملة"""
    try:
        async with websockets.connect(
            OMNISTON_WS_URL, ping_interval=20, ping_timeout=20
        ) as ws:
            # جربنا نرسل ping بسيط
            request_id = str(uuid.uuid4())
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "v1beta7.quote",
                "params": {
                    "bid_asset_address": {
                        "blockchain": BLOCKCHAIN_ID,
                        "address": "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c",
                    },
                    "ask_asset_address": {
                        "blockchain": BLOCKCHAIN_ID,
                        "address": TOKENS["NOT"]["addr"],
                    },
                    "amount": {"bid_units": "100000000"},  # 0.1 TON
                    "referrer_fee_bps": 0,
                    "settlement_methods": [0],
                },
            }
            await ws.send(json.dumps(payload))
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=max(0.1, deadline - time.time())
                    )
                    data = json.loads(raw)
                    if data.get("error"):
                        return f"❌ Omniston error: {str(data['error'])[:100]}"
                    if data.get("result"):
                        return f"✅ Omniston اشتغل! الرد: {str(data['result'])[:80]}"
                except asyncio.TimeoutError:
                    break
            return "⚠️ Omniston ما رد (timeout)"
    except Exception as e:
        return f"❌ فشل الاتصال: {str(e)[:120]}"

# ═══════════════════════════════════════
#   الحلقة الرئيسية
# ═══════════════════════════════════════
async def run_cycle(state):
    offset = state.get("last_update_id", 0) + 1
    updates = await get_updates(offset)
    for u in updates:
        state["last_update_id"] = u["update_id"]
        text = (u.get("message", {}).get("text") or "").strip().lower()
        if "حالة" in text:
            await send(
                f"📊 <b>حالة البوت</b>\n\n"
                f"🔍 الوضع: {state['phase']}\n"
                f"💼 صفقات: {len(state['trades'])}\n"
                f"🧪 نتيجة الاختبار: {state.get('omni_test_result', 'ما تم')}"
            )
        elif "اختبار" in text:
            await send("🧪 جاري اختبار Omniston...")
            result = await test_omni_connection()
            state["omni_test_result"] = result
            await send(f"<b>نتيجة الاختبار:</b>\n\n{result}")

    if state["phase"] == "paused":
        return state

    if state["phase"] == "hunting":
        best = await scan(state)
        if best and best["score"] >= 65:
            reasons_txt = "\n".join(f"• {r}" for r in best["reasons"][:5])
            mode_txt = "🧪 <b>وضع اختبار (DRY RUN)</b>" if DRY_RUN else "🔥 <b>وضع حقيقي</b>"
            await send(
                f"🚨 <b>إشارة قوية!</b>\n{mode_txt}\n\n"
                f"🪙 <b>{best['name']} ({best['symbol']})</b>\n"
                f"💰 ${best['price']:.6f}\n"
                f"📊 {best['score']:.0f}/100\n\n"
                f"📝 <b>الأسباب:</b>\n{reasons_txt}\n\n"
                f"{'🧪 ما راح نشتري (اختبار فقط)' if DRY_RUN else '🔥 جاري الشراء التلقائي...'}"
            )

            if DRY_RUN:
                await send("🧪 <b>DRY RUN:</b> لو كان حقيقي، كان شريت الآن. للحقيقي غير DRY_RUN=0 في Settings.")
            else:
                # هنا راح نضيف الشراء الحقيقي بعد ما نختبر
                await send("🔥 <b>الشراء الحقيقي قادم بالنسخة التالية</b>")

            state["phase"] = "hunting"  # نبقى نبحث
        else:
            if best:
                print(f"no signal ({best['symbol']}: {best['score']:.0f})")

    await daily_report(state)
    return state

async def daily_report(state):
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if state.get("last_daily_report") == today or now.hour < 20:
        return
    state["last_daily_report"] = today
    await send(f"📊 <b>التقرير اليومي</b>\n📅 {today}\n📈 صفقات: {len(state['trades'])}")

async def main():
    state = load_state()
    await send(
        "🚀 <b>البوت شغال - وضع الاختبار</b>\n\n"
        "🧪 ما راح يشتري شي. فقط يحلل ويراقب.\n"
        "✍️ اكتب: <b>اختبار</b> لفحص Omniston\n"
        "✍️ اكتب: <b>حالة</b> لعرض الوضع"
    )
    print("bot started")

    # أول شي: اختبار Omniston
    await send("🧪 جاري اختبار Omniston أول مرة...")
    result = await test_omni_connection()
    state["omni_test_result"] = result
    await send(f"<b>نتيجة اختبار Omniston:</b>\n\n{result}")

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
