import asyncio
import os
import time
import json
from datetime import datetime
from dotenv import load_dotenv
import httpx

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
STATE_FILE = "state.json"
CONFIRM_TIMEOUT = 1800

TOKENS = {
    "XROCK": "EQ...",
    "STORM": "EQ...",
    "UTYA": "EQ...",
    "LAMBO": "EQ...",
    "TAC": "EQ...",
    "GRAM": "EQ...",
    "STON": "EQ...",
    "NOT": "EQ...",
    "cbBTC": "EQ...",
    "WETH": "EQ...",
}

DEFAULT_STATE = {
    "phase": "hunting",
    "current_symbol": None,
    "entry_price": 0.0,
    "target_price": 0.0,
    "stop_price": 0.0,
    "sell_price": 0.0,
    "signal_sent_at": 0,
    "last_update_id": 0,
    "price_history": {},
    "trades": [],
    "last_daily_report": "",
}

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except: pass
    return dict(DEFAULT_STATE)

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)

async def send(text):
    if not BOT_TOKEN or not CHAT_ID:
        print(text); return
    try:
        async with httpx.AsyncClient() as c:
            await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"send fail: {e}")

async def get_updates(offset):
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 0}, timeout=15)
            return r.json().get("result", [])
    except: return []

async def get_price(addr):
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"https://api.dexscreener.com/latest/dex/search?q={addr}", timeout=10)
            data = r.json()
            if "pairs" in data and data["pairs"]:
                pairs = sorted(data["pairs"], key=lambda p: p.get("liquidity",{}).get("usd",0) or 0, reverse=True)
                p = pairs[0]
                return {"price": float(p.get("priceUsd",0) or 0),
                        "liquidity": float(p.get("liquidity",{}).get("usd",0) or 0)}
    except: pass
    return None

def ema(data, period):
    if len(data) < period: return data[-1] if data else 0
    k = 2/(period+1); e = sum(data[:period])/period
    for p in data[period:]: e = p*k + e*(1-k)
    return e

def rsi(prices, period=14):
    if len(prices) < period+1: return 50
    g, l = [], []
    for i in range(1, len(prices)):
        d = prices[i]-prices[i-1]; g.append(max(d,0)); l.append(max(-d,0))
    ag = sum(g[-period:])/period; al = sum(l[-period:])/period
    if al == 0: return 100
    return 100 - 100/(1+ag/al)

def boll(prices, period=20):
    if len(prices) < period: return 0.5
    r = prices[-period:]; m = sum(r)/period
    sd = (sum((p-m)**2 for p in r)/period)**0.5
    if sd == 0: return 0.5
    return (prices[-1] - (m-2*sd))/(4*sd)

def stoch(prices, period=14):
    if len(prices) < period: return 50
    r = prices[-period:]; h, l = max(r), min(r)
    if h == l: return 50
    return 100*(prices[-1]-l)/(h-l)

def score(prices):
    if len(prices) < 15: return 50, []
    r = rsi(prices); b = boll(prices); st = stoch(prices)
    e9 = ema(prices, 9); e21 = ema(prices, 21)
    s = 50; reasons = []
    if r < 35: s += 25; reasons.append(f"RSI={r:.0f}")
    elif r > 70: s -= 25; reasons.append(f"RSI={r:.0f}")
    if b < 0.2: s += 20; reasons.append("بولينجر قاع")
    elif b > 0.8: s -= 20; reasons.append("بولينجر قمة")
    if st < 20: s += 15; reasons.append("ستوكاستك منخفض")
    elif st > 80: s -= 15
    if e9 > e21: s += 10; reasons.append("EMA صاعد")
    else: s -= 10
    return max(0, min(100, s)), reasons

async def scan_all(state):
    best = None
    for sym, addr in TOKENS.items():
        if addr == "EQ...": continue
        md = await get_price(addr)
        if not md or md["price"] == 0: continue
        if sym not in state["price_history"]: state["price_history"][sym] = []
        state["price_history"][sym].append({"t": time.time(), "price": md["price"]})
        state["price_history"][sym] = state["price_history"][sym][-100:]
        prices = [h["price"] for h in state["price_history"][sym]]
        s, reasons = score(prices)
        if md["liquidity"] < 5000: s = 0
        if best is None or s > best["score"]:
            best = {"symbol": sym, "score": s, "price": md["price"], "reasons": reasons, "liquidity": md["liquidity"]}
    return best

async def check_current(state):
    sym = state["current_symbol"]
    addr = TOKENS.get(sym, "")
    if not addr or addr == "EQ...": return None
    md = await get_price(addr)
    if not md: return None
    prices = [h["price"] for h in state["price_history"].get(sym, [])]
    prices.append(md["price"])
    s, reasons = score(prices)
    pnl = (md["price"] - state["entry_price"]) / state["entry_price"] if state["entry_price"] > 0 else 0
    return {"symbol": sym, "price": md["price"], "score": s, "reasons": reasons, "pnl": pnl}

async def handle_messages(state):
    offset = state.get("last_update_id", 0) + 1
    updates = await get_updates(offset)
    for u in updates:
        state["last_update_id"] = u["update_id"]
        text = (u.get("message", {}).get("text") or "").strip().lower()
        if not text: continue

        if state["phase"] == "waiting_confirmation":
            if any(k in text for k in ["اشتريت", "شريت", "تم الشراء", "bought"]):
                md = await get_price(TOKENS[state["current_symbol"]])
                state["entry_price"] = md["price"] if md else 0
                state["target_price"] = state["entry_price"] * 1.25
                state["stop_price"] = state["entry_price"] * 0.90
                state["phase"] = "holding"
                await send(f"✅ تم تسجيل الشراء\n💰 سعر الدخول: ${state['entry_price']:.6f}\n🎯 الهدف: +25%\n🛑 الوقف: -10%\n\n📡 البوت يراقب...")
            elif any(k in text for k in ["الغاء", "تخطى", "skip", "لا"]):
                state["phase"] = "hunting"; state["current_symbol"] = None
                await send("👌 تم الإلغاء. البوت يرجع يبحث.")

        elif state["phase"] == "waiting_sell_confirmation":
            if any(k in text for k in ["تم البيع", "بعت", "بعته", "sold"]):
                pnl = (state.get("sell_price", 0) - state["entry_price"]) / state["entry_price"] if state["entry_price"] > 0 else 0
                state["trades"].append({"symbol": state["current_symbol"], "entry": state["entry_price"],
                    "exit": state.get("sell_price", 0), "pnl": pnl, "at": time.time()})
                state["phase"] = "hunting"; state["current_symbol"] = None; state["entry_price"] = 0
                await send(f"✅ تم تسجيل البيع | ربح: {pnl*100:+.1f}%\n🔍 البوت يرجع يبحث...")
            elif any(k in text for k in ["استمر", "hold", "لا"]):
                state["phase"] = "holding"
                await send("👌 تم التأجيل. البوت يكمل مراقبة.")

        elif state["phase"] == "holding" and any(k in text for k in ["حالة", "status", "تقرير"]):
            cur = await check_current(state)
            if cur:
                await send(f"📊 {cur['symbol']}\n💰 ${cur['price']:.6f}\n📈 {cur['pnl']*100:+.1f}%")

        elif state["phase"] == "hunting" and any(k in text for k in ["حالة", "status"]):
            await send(f"🔍 البوت يبحث عن فرصة\n📊 عدد الصفقات: {len(state['trades'])}")

    return state

async def daily_report(state):
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if state.get("last_daily_report") == today: return
    if now.hour < 20: return
    state["last_daily_report"] = today
    trades = state["trades"]
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] <= 0)
    total_pnl = sum(t["pnl"] for t in trades)
    await send(f"📊 <b>تقرير يومي</b>\n📅 {today}\n\n🏆 رابحة: {wins}\n🔻 خاسرة: {losses}\n💵 PnL: {total_pnl*100:+.1f}%\n🔍 الحالة: {state['phase']}")

async def run_cycle():
    state = load_state()
    state = await handle_messages(state)

    if state["phase"] == "hunting":
        best = await scan_all(state)
        if best and best["score"] >= 70:
            state["phase"] = "waiting_confirmation"
            state["current_symbol"] = best["symbol"]
            state["signal_sent_at"] = time.time()
            await send(
                f"🚨 <b>إشارة قوية!</b>\n\n"
                f"🪙 {best['symbol']}\n"
                f"💰 ${best['price']:.6f}\n"
                f"📊 {best['score']}/100\n"
                f"📝 {', '.join(best['reasons'][:4])}\n"
                f"💧 ${best['liquidity']:.0f}\n\n"
                f"🟢 <b>اشتري بكل فلوسك الآن</b>\n"
                f"✍️ لما تشتري اكتب: <b>اشتريت</b>\n❌ للتخطي: <b>الغاء</b>"
            )
        else:
            print(f"No signal (best: {best['score'] if best else 0})")

    elif state["phase"] == "waiting_confirmation":
        if time.time() - state["signal_sent_at"] > CONFIRM_TIMEOUT:
            await send("⏰ ما وصلني تأكيد. ألغي وأرجع أبحث.")
            state["phase"] = "hunting"; state["current_symbol"] = None

    elif state["phase"] == "holding":
        cur = await check_current(state)
        if cur:
            if cur["price"] >= state["target_price"] or cur["price"] <= state["stop_price"] or cur["score"] <= 30:
                reason = "🎯 وصل الهدف" if cur["price"] >= state["target_price"] else ("🛑 وصل الوقف" if cur["price"] <= state["stop_price"] else "📉 المؤشرات انقلبت")
                state["phase"] = "waiting_sell_confirmation"
                state["sell_price"] = cur["price"]
                await send(
                    f"🔴 <b>وقت البيع!</b>\n\n"
                    f"🪙 {cur['symbol']}\n"
                    f"💰 ${cur['price']:.6f}\n"
                    f"📈 {cur['pnl']*100:+.1f}%\n"
                    f"📝 {reason}\n\n"
                    f"✍️ لما تبيع اكتب: <b>تم البيع</b>\n⏸️ للتأجيل: <b>استمر</b>"
                )
            else:
                print(f"Holding {cur['symbol']}: {cur['pnl']*100:+.1f}%")

    await daily_report(state)
    save_state(state)

if __name__ == "__main__":
    asyncio.run(run_cycle())
