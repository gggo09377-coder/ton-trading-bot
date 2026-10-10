import asyncio
import json
import os
import time
from datetime import datetime
from urllib.parse import quote
from dotenv import load_dotenv
import httpx

try:
    from wallet_risk import (
        config as wallet_config,
        get_portfolio_usd,
        get_ton_balance,
        get_ton_usd_price,
        calculate_position,
        pause as wallet_pause,
        resume as wallet_resume,
        set_manual_balance,
        set_wallet,
    )
    WALLET_OK = True
except Exception as e:
    print(f"wallet_risk load failed: {e}")
    WALLET_OK = False

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

STATE_FILE = "state.json"
CYCLE_SLEEP = 60
MIN_SCORE = 75
STRONG_SCORE = 85
MIN_LIQUIDITY = 10000
COOLDOWN = 900
DAILY_MAX_SIGNALS = 20

TOKENS = {
    "NOT":  {"addr": "EQAvlWFDxGF2lXm67y4yzC17wYKD9A0guwPkMs1gOsM__NOT", "name": "Notcoin"},
    "DOGS": {"addr": "EQCvxJy4eG8hyHBFsZ7eePxrRsUQSFE_jpptRAYBmcG_DOGS", "name": "Dogs"},
    "STON": {"addr": "EQA2kCVNwVsil2EM2mB0SkXytxCqQjS4mttjDpnXmwG9T6bO", "name": "STON.fi"},
}

DEFAULT_STATE = {
    "last_update_id": 0,
    "price_history": {},
    "volume_history": {},
    "sent_signals": [],
    "last_report": "",
    "stats": {"signals_sent": 0},
}


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            state = dict(DEFAULT_STATE)
            state.update(saved)
            for k in ("price_history", "volume_history"):
                if not isinstance(state.get(k), dict):
                    state[k] = {}
            if not isinstance(state.get("sent_signals"), list):
                state["sent_signals"] = []
            if not isinstance(state.get("stats"), dict):
                state["stats"] = {"signals_sent": 0}
            state["stats"].setdefault("signals_sent", 0)
            state["last_update_id"] = int(state.get("last_update_id", 0) or 0)
            return state
        except Exception as e:
            print(f"state load fail: {e}")
    return dict(DEFAULT_STATE)


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"save fail: {e}")


async def send(message):
    if not BOT_TOKEN or not CHAT_ID:
        print(message)
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    chunks = []
    remaining = message
    while len(remaining) > 4000:
        cut = remaining.rfind("\n", 0, 4000)
        if cut < 1000:
            cut = 4000
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            for chunk in chunks:
                r = await c.post(url, data={"chat_id": CHAT_ID, "text": chunk, "parse_mode": "HTML"})
                r.raise_for_status()
                if not r.json().get("ok"):
                    return False
        return True
    except Exception as e:
        print(f"send fail: {e}")
        return False


async def get_updates(offset):
    if not BOT_TOKEN:
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 0, "allowed_updates": json.dumps(["message"])},
            )
            r.raise_for_status()
            data = r.json()
            return data.get("result", []) if data.get("ok") else []
    except Exception as e:
        print(f"getUpdates fail: {e}")
        return []


def norm_addr(a):
    return str(a or "").split("_")[0].strip().lower()


async def get_price(addr):
    url = "https://api.dexscreener.com/token-pairs/v1/ton/" + quote(addr, safe="")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(url)
            r.raise_for_status()
            data = r.json()
        if not isinstance(data, list):
            return None
        wanted = norm_addr(addr)
        valid = []
        for p in data:
            if not isinstance(p, dict):
                continue
            if str(p.get("chainId", "")).lower() != "ton":
                continue
            base = p.get("baseToken") or {}
            quote_t = p.get("quoteToken") or {}
            ba = norm_addr(base.get("address"))
            qa = norm_addr(quote_t.get("address"))
            if wanted not in (ba, qa):
                continue
            try:
                price = float(p.get("priceUsd") or 0)
                liq = float((p.get("liquidity") or {}).get("usd") or 0)
                vol = float((p.get("volume") or {}).get("h24") or 0)
                ch = p.get("priceChange") or {}
                c1 = float(ch.get("h1") or 0)
                c24 = float(ch.get("h24") or 0)
            except (ValueError, TypeError):
                continue
            if price <= 0:
                continue
            valid.append({"price": price, "volume": vol, "liquidity": liq, "change_1h": c1, "change_24h": c24})
        if not valid:
            return None
        return max(valid, key=lambda x: x["liquidity"])
    except Exception as e:
        print(f"price fail: {e}")
        return None


def ema(data, period):
    if not data:
        return 0.0
    if len(data) < period:
        return sum(data) / len(data)
    k = 2 / (period + 1)
    v = sum(data[:period]) / period
    for x in data[period:]:
        v = x * k + v * (1 - k)
    return v


def rsi(data, period=14):
    if len(data) < period + 1:
        return 50.0
    changes = [data[i] - data[i - 1] for i in range(len(data) - period, len(data))]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]
    ag = sum(gains) / period
    al = sum(losses) / period
    if ag == 0 and al == 0:
        return 50.0
    if al == 0:
        return 100.0
    if ag == 0:
        return 0.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def macd_calc(data):
    if len(data) < 35:
        return {"macd": 0.0, "signal": 0.0, "hist": 0.0}
    series = []
    for i in range(26, len(data) + 1):
        series.append(ema(data[:i], 12) - ema(data[:i], 26))
    m = series[-1]
    s = ema(series, 9)
    return {"macd": m, "signal": s, "hist": m - s}


def bollinger(data, period=20):
    if len(data) < period:
        return {"pos": 0.5}
    values = data[-period:]
    mean = sum(values) / period
    var = sum((x - mean) ** 2 for x in values) / period
    std = var ** 0.5
    if std == 0:
        return {"pos": 0.5}
    up = mean + 2 * std
    lo = mean - 2 * std
    return {"pos": (data[-1] - lo) / (up - lo)}


def weighted_score(prices, volumes):
    if len(prices) < 35:
        return 0.0, ["بيانات غير كافية"]
    reasons = []
    ws = 0.0
    tw = 0.0

    r = rsi(prices)
    if r < 30:
        s = 100; reasons.append(f"RSI منخفض جدًا ({r:.0f})")
    elif r < 40:
        s = 70; reasons.append(f"RSI منخفض ({r:.0f})")
    elif r > 70:
        s = 10
    else:
        s = 40
    ws += s * 30; tw += 30

    m = macd_calc(prices)
    if m["hist"] > 0 and m["macd"] > m["signal"]:
        s = 100; reasons.append("MACD صاعد")
    elif m["hist"] > 0:
        s = 60
    else:
        s = 15
    ws += s * 25; tw += 25

    b = bollinger(prices)
    if b["pos"] < 0.15:
        s = 100; reasons.append("قاع بولينجر")
    elif b["pos"] < 0.3:
        s = 65
    elif b["pos"] > 0.85:
        s = 10
    else:
        s = 40
    ws += s * 20; tw += 20

    if len(volumes) >= 6:
        avg = sum(volumes[-6:-1]) / 5
        if avg > 0:
            ratio = volumes[-1] / avg
            if ratio > 2.5:
                s = 100; reasons.append(f"حجم x{ratio:.1f}")
            elif ratio > 1.5:
                s = 65
            else:
                s = 40
            ws += s * 15; tw += 15

    e9 = ema(prices, 9)
    e21 = ema(prices, 21)
    if e9 > e21:
        s = 80; reasons.append("EMA صاعد")
    else:
        s = 20
    ws += s * 10; tw += 10

    final = ws / tw if tw else 0
    return min(100.0, max(0.0, final)), reasons


async def scan(state):
    best = None
    all_results = []
    for sym, info in TOKENS.items():
        md = await get_price(info["addr"])
        if not md or md["price"] <= 0:
            continue
        state["price_history"].setdefault(sym, [])
        state["volume_history"].setdefault(sym, [])
        state["price_history"][sym].append({"t": time.time(), "price": md["price"]})
        state["price_history"][sym] = state["price_history"][sym][-100:]
        state["volume_history"][sym].append(md["volume"])
        state["volume_history"][sym] = state["volume_history"][sym][-100:]

        prices = [x["price"] for x in state["price_history"][sym] if isinstance(x, dict) and x.get("price", 0) > 0]
        volumes = state["volume_history"][sym]
        sc, reasons = weighted_score(prices, volumes)

        if md["liquidity"] < MIN_LIQUIDITY:
            sc = 0
            reasons.append("سيولة ضعيفة")

        cand = {
            "symbol": sym, "name": info["name"], "score": sc,
            "price": md["price"], "reasons": reasons,
            "liquidity": md["liquidity"],
            "change_1h": md["change_1h"], "change_24h": md["change_24h"],
        }
        all_results.append(cand)
        if best is None or cand["score"] > best["score"]:
            best = cand
    return best, all_results


def daily_count(state):
    today = datetime.now().strftime("%Y-%m-%d")
    return sum(1 for s in state["sent_signals"] if s.get("date") == today)


async def send_signal(state, best):
    entry = best["price"]
    tp = entry * 1.25
    sl = entry * 0.92

    balance_usd = None
    position_info = None
    if WALLET_OK:
        try:
            balance_usd = float(await get_portfolio_usd())
            if balance_usd > 0:
                position_info = calculate_position(
                    balance_usd=balance_usd,
                    entry=entry,
                    stop=sl,
                    estimated_cost_pct=1.5,
                )
        except Exception as e:
            print(f"position calc fail: {e}")

    is_strong = best["score"] >= STRONG_SCORE

    if is_strong:
        if balance_usd:
            size_usd = balance_usd * 0.9
            advice = f"🔥 <b>ادخل بكل فلوسك!</b>\n💰 المبلغ المقترح: <b>${size_usd:.2f}</b> (90% من رصيدك)"
        else:
            advice = "🔥 <b>ادخل بكل فلوسك!</b>\n⚠️ حدد رصيدك أول: /setbalance 6.00"
    else:
        if position_info:
            advice = f"🟢 ادخل بحجم: <b>${position_info['position_usd']:.2f}</b>"
        else:
            advice = f"🟢 ادخل بحجم معتدل"

    reasons_txt = "\n".join(f"• {r}" for r in best["reasons"][:5]) or "• لا توجد أسباب كافية"

    balance_line = ""
    if balance_usd:
        balance_line = f"💼 رصيدك: ${balance_usd:.2f}\n"

    message = (
        f"🚨 <b>إشارة {'قوية جداً' if is_strong else 'قوية'} - {best['name']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"🪙 <b>{best['symbol']}</b>\n"
        f"💰 السعر: <code>${entry:.8f}</code>\n"
        f"📊 النقاط: <b>{best['score']:.0f}/100</b>\n"
        f"💧 السيولة: ${best['liquidity']:,.0f}\n"
        f"{balance_line}\n"
        f"🎯 <b>الهدف (+25%):</b> <code>${tp:.8f}</code>\n"
        f"🛑 <b>الوقف (-8%):</b> <code>${sl:.8f}</code>\n\n"
        f"{advice}\n\n"
        f"📝 <b>الأسباب:</b>\n{reasons_txt}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ إشارة آلية. ليست ضماناً للربح."
    )

    sent = await send(message)
    if not sent:
        return

    state["sent_signals"].append({
        "symbol": best["symbol"], "price": entry,
        "tp": tp, "sl": sl, "t": time.time(),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "score": best["score"],
    })
    state["sent_signals"] = state["sent_signals"][-200:]
    state["stats"]["signals_sent"] += 1


async def handle_messages(state):
    updates = await get_updates(state.get("last_update_id", 0) + 1)
    for u in updates:
        state["last_update_id"] = max(state.get("last_update_id", 0), u.get("update_id", 0))
        msg = u.get("message") or {}
        chat = msg.get("chat") or {}
        if not CHAT_ID or str(chat.get("id", "")) != CHAT_ID:
            continue
        text = (msg.get("text") or "").strip()
        low = text.lower()

        if low in ("حالة", "/status"):
            daily = daily_count(state)
            bal_line = ""
            if WALLET_OK:
                try:
                    b = await get_portfolio_usd()
                    bal_line = f"💼 الرصيد: ${float(b):.2f}\n"
                except Exception:
                    bal_line = "💼 الرصيد: غير محدد\n"
            await send(
                f"📊 <b>حالة البوت</b>\n\n"
                f"{bal_line}"
                f"📨 إشارات اليوم: {daily}/{DAILY_MAX_SIGNALS}\n"
                f"📨 الإجمالي: {state['stats']['signals_sent']}\n"
                f"🎯 الحد: {MIN_SCORE}/100 (قوي جداً: {STRONG_SCORE}+)"
            )

        elif low in ("اختبار", "/test"):
            await send("🧪 جاري فحص العملات...")
            best, all_r = await scan(state)
            if best:
                txt = f"🧪 <b>نتيجة الفحص ({len(all_r)} عملة)</b>\n\n"
                for r in sorted(all_r, key=lambda x: x["score"], reverse=True):
                    txt += f"• <b>{r['symbol']}</b>: {r['score']:.0f}/100 | ${r['price']:.8f}\n"
                await send(txt)
            else:
                await send("⚠️ ما وصلت بيانات")

        elif low.startswith("/setbalance"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                await send("استخدام: /setbalance 6.00")
                continue
            try:
                if WALLET_OK:
                    amt = set_manual_balance(parts[1])
                    await send(f"✅ تم تعيين الرصيد: ${float(amt):.2f}")
                else:
                    await send("⚠️ wallet_risk غير محمّل")
            except Exception as e:
                await send(f"❌ خطأ: {e}")

        elif low.startswith("/wallet"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                await send("استخدام: /wallet <عنوان المحفظة>")
                continue
            try:
                if WALLET_OK:
                    set_wallet(parts[1])
                    await send("✅ تم تعيين المحفظة")
                else:
                    await send("⚠️ wallet_risk غير محمّل")
            except Exception as e:
                await send(f"❌ خطأ: {e}")

        elif low in ("/balance", "رصيد"):
            if not WALLET_OK:
                await send("⚠️ wallet_risk غير محمّل")
                continue
            try:
                b = await get_portfolio_usd()
                ton = None
                try:
                    ton = await get_ton_balance()
                except Exception:
                    pass
                txt = f"💼 <b>الرصيد</b>\n\nالقيمة: ${float(b):.2f}"
                if ton is not None:
                    txt += f"\nTON: {float(ton):.4f}"
                await send(txt)
            except Exception as e:
                await send(f"❌ خطأ: {e}")

        elif low in ("/pause", "ايقاف"):
            if WALLET_OK:
                wallet_pause()
            await send("⏸️ تم إيقاف الإشارات")

        elif low in ("/resume", "تشغيل"):
            if WALLET_OK:
                wallet_resume()
            await send("▶️ تم استئناف الإشارات")

    return state


async def run_cycle(state):
    await handle_messages(state)

    daily = daily_count(state)
    if daily >= DAILY_MAX_SIGNALS:
        return state

    best, _ = await scan(state)
    if best:
        print(f"scan: {best['symbol']}={best['score']:.0f}")
        if best["score"] >= MIN_SCORE:
            now = time.time()
            recent = [
                s for s in state["sent_signals"]
                if s.get("symbol") == best["symbol"]
                and now - s.get("t", 0) < COOLDOWN
            ]
            if not recent:
                await send_signal(state, best)

    return state


async def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("خطأ: عيّن BOT_TOKEN و CHAT_ID")
        return

    state = load_state()

    await send(
        "🚀 <b>بوت التحليل شغال</b>\n\n"
        f"🪙 العملات: {len(TOKENS)}\n"
        f"🎯 الحد الأدنى: {MIN_SCORE}/100\n"
        f"🔥 إشارة قوية جداً: {STRONG_SCORE}+ (ادخل بكل فلوسك)\n"
        f"📊 حد يومي: {DAILY_MAX_SIGNALS} إشارات\n\n"
        "<b>الأوامر:</b>\n"
        "/balance - عرض الرصيد\n"
        "/setbalance - تعيين الرصيد\n"
        "/wallet - تعيين المحفظة\n"
        "/test - فحص فوري\n"
        "/status - حالة البوت\n"
        "/pause - إيقاف\n"
        "/resume - استئناف"
    )

    print("bot started")
    end_time = time.monotonic() + 5 * 3600 + 50 * 60
    while time.monotonic() < end_time:
        try:
            await run_cycle(state)
        except Exception as e:
            print(f"cycle error: {type(e).__name__}: {e}")
        save_state(state)
        await asyncio.sleep(CYCLE_SLEEP)
    save_state(state)
    print("done")


if __name__ == "__main__":
    asyncio.run(main())
