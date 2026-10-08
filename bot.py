import asyncio
import os
import time
import json
import statistics
from datetime import datetime
from dotenv import load_dotenv
from tonsdk.contract.wallet import Wallets, WalletVersionEnum
from tonsdk.utils import Address, bytes_to_b64str
from dedust import Asset, Factory, PoolType, SwapParams, VaultJetton, VaultNative
from dedust.provider import Provider
import httpx

load_dotenv()

MNEMONIC = os.getenv("TON_MNEMONIC", "").split()
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
STARTING_CAPITAL = float(os.getenv("STARTING_CAPITAL", "6.0"))
MAX_TON_PER_TRADE = float(os.getenv("MAX_TON_PER_TRADE", "0.5"))
MIN_TON_RESERVE = float(os.getenv("MIN_TON_RESERVE", "1.5"))
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.15"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "180"))
MIN_SCORE_TO_BUY = int(os.getenv("MIN_SCORE_TO_BUY", "65"))

STATE_FILE = "state.json"

HALAL_WHITELIST = {
    "XROCK": True, "STORM": True, "UTYA": True, "LAMBO": True,
    "TAC": True, "GRAM": True, "STON": True, "NOT": True,
    "cbBTC": True, "WETH": True, "RAFF": False, "REDO": False,
    "MY": False, "PX": False, "durev": False, "ATF": False,
}

TOKENS = {
    "XROCK":  {"addr": "EQ...", "term": "short"},
    "STORM":  {"addr": "EQ...", "term": "short"},
    "UTYA":   {"addr": "EQ...", "term": "short"},
    "LAMBO":  {"addr": "EQ...", "term": "short"},
    "TAC":    {"addr": "EQ...", "term": "short"},
    "GRAM":   {"addr": "EQ...", "term": "medium"},
    "STON":   {"addr": "EQ...", "term": "medium"},
    "NOT":    {"addr": "EQ...", "term": "medium"},
    "cbBTC":  {"addr": "EQ...", "term": "long"},
    "WETH":   {"addr": "EQ...", "term": "long"},
}

mnemonics, pub_k, priv_k, wallet = Wallets.from_mnemonics(
    mnemonics=MNEMONIC, version=WalletVersionEnum.v4r2, workchain=0
)
WALLET_ADDRESS = wallet.address.to_string(is_user_friendly=True, is_bounceable=True)

state = {
    "positions": {}, "history": [], "total_pnl_ton": 0.0,
    "daily_start": None, "daily_date": None, "weekly_start": None, "weekly_date": None,
    "cycles": 0,
    "stats": {"wins": 0, "losses": 0, "gross_profit": 0.0, "gross_loss": 0.0, "peak_balance": STARTING_CAPITAL, "max_drawdown": 0.0}
}
price_history = {}

def load_state():
    global state, price_history
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                d = json.load(f); state = d.get("state", state); price_history = d.get("price_history", {})
        except: pass

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"state": state, "price_history": price_history}, f, indent=2)
    except: pass

async def notify(msg):
    if not BOT_TOKEN or not CHAT_ID: print(msg); return
    try:
        async with httpx.AsyncClient() as c:
            await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e: print(f"إشعار فشل: {e}")

async def get_market_data(token_addr):
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"https://api.dexscreener.com/latest/dex/search?q={token_addr}", timeout=10)
            data = r.json()
            if "pairs" in data and data["pairs"]:
                pairs = sorted(data["pairs"], key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0, reverse=True)
                p = pairs[0]
                return {"price": float(p.get("priceUsd", 0) or 0), "volume_24h": float(p.get("volume", {}).get("h24", 0) or 0), "volume_1h": float(p.get("volume", {}).get("h1", 0) or 0), "liquidity": float(p.get("liquidity", {}).get("usd", 0) or 0), "change_1h": float(p.get("priceChange", {}).get("h1", 0) or 0), "change_24h": float(p.get("priceChange", {}).get("h24", 0) or 0)}
    except: pass
    return None

async def get_ton_balance(provider):
    try: r = await provider.runGetMethod(address=wallet.address, method="get_wallet_data"); return int(r[0]["value"]) / 1e9
    except: return 0.0

async def get_jetton_balance(provider, jetton_addr):
    try:
        jw = await provider.runGetMethod(address=Address(jetton_addr), method="get_wallet_address", stack=[["slice", wallet.address.to_string()]])
        wallet_addr = jw[0]["value"]; bal = await provider.runGetMethod(address=Address(wallet_addr), method="get_wallet_data"); return int(bal[0]["value"])
    except: return 0

def sma(data, period):
    if len(data) < period: return 0
    return sum(data[-period:]) / period

def ema(data, period):
    if len(data) < period: return data[-1] if data else 0
    k = 2 / (period + 1); e = sum(data[:period]) / period
    for p in data[period:]: e = p * k + e * (1 - k)
    return e

def calc_rsi(prices, period=14):
    if len(prices) < period + 1: return 50
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i-1]; gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period; al = sum(losses[-period:]) / period
    if al == 0: return 100
    return 100 - (100 / (1 + ag / al))

def calc_macd(prices):
    if len(prices) < 26: return {"macd": 0, "signal": 0, "hist": 0}
    ema12 = ema(prices, 12); ema26 = ema(prices, 26); m = ema12 - ema26
    macd_series = [ema(prices[:i], 12) - ema(prices[:i], 26) for i in range(26, len(prices) + 1)]
    sig = ema(macd_series, 9) if len(macd_series) >= 9 else m
    return {"macd": m, "signal": sig, "hist": m - sig}

def calc_bollinger(prices, period=20):
    if len(prices) < period: return {"upper": 0, "lower": 0, "mid": 0, "pos": 0.5, "width": 0}
    recent = prices[-period:]; mid = sum(recent) / period; var = sum((p - mid) ** 2 for p in recent) / period; sd = var ** 0.5
    upper = mid + 2 * sd; lower = mid - 2 * sd; width = (upper - lower) / mid if mid > 0 else 0
    if upper == lower: return {"upper": upper, "lower": lower, "mid": mid, "pos": 0.5, "width": 0}
    pos = (prices[-1] - lower) / (upper - lower); return {"upper": upper, "lower": lower, "mid": mid, "pos": pos, "width": width}

def calc_adx(prices, period=14):
    if len(prices) < period + 1: return 0
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(prices)):
        up = prices[i] - prices[i-1]; down = prices[i-1] - prices[i]
        plus_dm.append(up if up > down and up > 0 else 0); minus_dm.append(down if down > up and down > 0 else 0); trs.append(abs(prices[i] - prices[i-1]))
    atr = sum(trs[-period:]) / period if trs else 1
    if atr == 0: return 0
    pdi = 100 * (sum(plus_dm[-period:]) / period) / atr; mdi = 100 * (sum(minus_dm[-period:]) / period) / atr
    if pdi + mdi == 0: return 0
    return 100 * abs(pdi - mdi) / (pdi + mdi)

def calc_stochastic(prices, period=14):
    if len(prices) < period: return 50
    recent = prices[-period:]; h, l = max(recent), min(recent)
    if h == l: return 50
    return 100 * (prices[-1] - l) / (h - l)

def calc_obv(price_vol_pairs):
    if len(price_vol_pairs) < 2: return 0
    obv = 0
    for i in range(1, len(price_vol_pairs)):
        prev_p = price_vol_pairs[i-1]["price"]; curr_p = price_vol_pairs[i]["price"]; vol = price_vol_pairs[i].get("volume", 0)
        if curr_p > prev_p: obv += vol
        elif curr_p < prev_p: obv -= vol
    return obv

def detect_market_regime(hist):
    if len(hist) < 30: return "unknown"
    prices = [h["price"] for h in hist[-30:]]; ema_fast = ema(prices, 9); ema_slow = ema(prices, 21)
    adx = calc_adx(prices); atr = calc_atr(prices, prices, prices); volatility = atr / prices[-1] if prices[-1] > 0 else 0
    if volatility > 0.08: return "volatile"
    if adx > 25 and ema_fast > ema_slow: return "bullish"
    if adx > 25 and ema_fast < ema_slow: return "bearish"
    if adx < 20: return "ranging"
    return "neutral"

def calc_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1: return 0
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])) for i in range(1, len(closes))]
    return sum(trs[-period:]) / period

def score_signal(symbol, term, md, hist, regime):
    if len(hist) < 30: return {"score": 0, "reasons": ["بيانات غير كافية"], "regime": regime}
    prices = [h["price"] for h in hist[-100:]]; reasons = []; score = 0
    if regime == "volatile": return {"score": 0, "reasons": ["تقلب عالي - تجنب"], "regime": regime}
    
    r = calc_rsi(prices)
    if regime == "bearish":
        if r < 25: score += 20; reasons.append(f"RSI ذروة بيع {r:.0f}")
        elif r < 35: score += 12
    else:
        if 40 < r < 65: score += 15; reasons.append(f"RSI صحي {r:.0f}")
        elif r < 30: score += 20; reasons.append(f"RSI ذروة بيع {r:.0f}")
        elif r > 78: score -= 20
    
    m = calc_macd(prices)
    if m["hist"] > 0 and m["macd"] > 0: score += 15; reasons.append("MACD إيجابي")
    elif m["hist"] < 0 and m["macd"] < 0: score -= 10
    
    ema9 = ema(prices, 9); ema21 = ema(prices, 21); ema50 = ema(prices, 50) if len(prices) >= 50 else ema21
    if ema9 > ema21 > ema50: score += 15; reasons.append("EMA متوافقة صعود")
    elif ema9 < ema21 < ema50: score -= 10
    
    b = calc_bollinger(prices)
    if b["pos"] < 0.15: score += 18; reasons.append("قاع بولينجر")
    elif b["pos"] < 0.3: score += 10
    elif b["pos"] > 0.9: score -= 15; reasons.append("قمة بولينجر")
    
    adx = calc_adx(prices)
    if adx > 30: score += 10; reasons.append(f"ADX قوي {adx:.0f}")
    elif adx > 20: score += 5
    
    st = calc_stochastic(prices)
    if st < 20: score += 10; reasons.append("ستوكاستك منخفض")
    elif st > 85: score -= 10
    
    if md["volume_24h"] > 0:
        vol_ratio = md["volume_1h"] / (md["volume_24h"] / 24) if md["volume_24h"] > 0 else 1
        if vol_ratio > 2: score += 10; reasons.append(f"حجم مرتفع x{vol_ratio:.1f}")
        elif vol_ratio > 1.3: score += 5
        elif vol_ratio < 0.5: score -= 5
    
    obv = calc_obv(hist[-20:])
    if obv > 0: score += 5
    elif obv < 0: score -= 3
    
    if term == "short":
        if md["change_1h"] > 2: score += 10; reasons.append(f"زخم 1س +{md['change_1h']:.1f}%")
        elif md["change_1h"] < -5: score -= 8
    elif term == "medium":
        if md["change_24h"] > 5: score += 8
    else:
        if md["change_24h"] < -10: score += 15; reasons.append("تراكم فرصة")
        elif md["change_24h"] > 20: score -= 10
    
    if md["liquidity"] < 3000: score = 0; reasons.append("سيولة ضعيفة جداً")
    elif md["liquidity"] < 10000: score -= 15; reasons.append("سيولة متوسطة")
    elif md["liquidity"] > 100000: score += 5; reasons.append("سيولة ممتازة")
    
    if regime == "bullish": score += 5
    elif regime == "bearish": score -= 3
    
    return {"score": max(0, min(100, score)), "reasons": reasons, "regime": regime, "rsi": r, "adx": adx, "boll_pos": b["pos"]}

def calculate_position_size(md, score, ton_balance, term):
    base = MAX_TON_PER_TRADE
    liq = md["liquidity"]
    if liq > 100000: liq_mult = 1.0
    elif liq > 50000: liq_mult = 0.8
    elif liq > 20000: liq_mult = 0.6
    elif liq > 10000: liq_mult = 0.4
    else: liq_mult = 0.25
    score_mult = min(1.5, (score - 50) / 50) if score > 50 else 0.3
    term_mult = {"short": 0.7, "medium": 1.0, "long": 1.3}[term]
    size = base * liq_mult * score_mult * term_mult
    size = max(0.1, min(size, base * 1.2))
    if ton_balance - MIN_TON_RESERVE - size < 0: return 0
    return round(size, 3)

def get_tp_sl_levels(term, score, entry_price):
    if term == "short":
        tp, sl = (0.20, 0.06) if score >= 80 else (0.15, 0.07) if score >= 70 else (0.10, 0.05)
    elif term == "medium":
        tp, sl = (0.50, 0.10) if score >= 80 else (0.35, 0.12) if score >= 70 else (0.25, 0.10)
    else:
        tp, sl = (1.00, 0.20) if score >= 80 else (0.70, 0.25) if score >= 70 else (0.50, 0.20)
    return {"tp": entry_price * (1 + tp), "sl": entry_price * (1 - sl), "tp_pct": tp, "sl_pct": sl, "scale_out_1": entry_price * (1 + tp * 0.4), "scale_out_2": entry_price * (1 + tp * 0.7), "breakeven_at": entry_price * (1 + tp * 0.3)}

async def execute_buy(symbol, ton_amount, token_addr):
    try:
        provider = Provider(); TON = Asset.native(); TOKEN = Asset.jetton(Address(token_addr))
        pool = await Factory.get_pool(pool_type=PoolType.VOLATILE, assets=[TON, TOKEN], provider=provider)
        swap_params = SwapParams(deadline=int(time.time() + 300), recipient_address=wallet.address)
        payload = VaultNative.create_swap_payload(amount=int(ton_amount * 1e9), pool_address=pool.address, swap_params=swap_params, limit=0)
        seqno = await provider.runGetMethod(address=wallet.address, method="seqno")
        query = wallet.create_transfer_message(to_addr=Address(pool.address), amount=int((ton_amount + 0.25) * 1e9), seqno=seqno[0]["value"], payload=payload)
        boc = bytes_to_b64str(query["message"].to_boc(False)); await provider.sendBoc(boc); return True
    except Exception as e: await notify(f"❌ فشل شراء {symbol}: {str(e)[:80]}"); return False

async def execute_sell(symbol, amount_nano, token_addr):
    try:
        provider = Provider(); TON = Asset.native(); TOKEN = Asset.jetton(Address(token_addr))
        pool = await Factory.get_pool(pool_type=PoolType.VOLATILE, assets=[TON, TOKEN], provider=provider)
        swap_params = SwapParams(deadline=int(time.time() + 300), recipient_address=wallet.address)
        payload = VaultJetton.create_swap_payload(amount=amount_nano, pool_address=pool.address, swap_params=swap_params, limit=0)
        seqno = await provider.runGetMethod(address=wallet.address, method="seqno")
        query = wallet.create_transfer_message(to_addr=Address(pool.address), amount=int(0.25 * 1e9), seqno=seqno[0]["value"], payload=payload)
        boc = bytes_to_b64str(query["message"].to_boc(False)); await provider.sendBoc(boc); return True
    except Exception as e: await notify(f"❌ فشل بيع {symbol}: {str(e)[:80]}"); return False

async def manage_position(symbol, info, md, pos):
    pnl = (md["price"] - pos["buy_price"]) / pos["buy_price"]; levels = pos["levels"]; term = pos["term"]; action = None
    if pnl >= levels["tp_pct"] * 0.3 and not pos.get("breakeven_moved"):
        pos["sl_price"] = pos["buy_price"] * 1.005; pos["breakeven_moved"] = True; action = f"🛡️ {symbol}: نقل الوقف لنقطة الدخول"
    if pnl >= levels["tp_pct"] * 0.5:
        new_sl = md["price"] * (1 - levels["sl_pct"] * 0.5)
        if new_sl > pos.get("sl_price", 0): pos["sl_price"] = new_sl; pos["trailing"] = True
    if pnl >= levels["tp_pct"] * 0.4 and not pos.get("sold_1"):
        provider = Provider(); bal = await get_jetton_balance(provider, info["addr"])
        if bal > 0:
            part = int(bal * 0.33)
            if part > 0:
                ok = await execute_sell(symbol, part, info["addr"])
                if ok: pos["sold_1"] = True; action = f"💵 {symbol}: بيع 33% @ +{pnl*100:.1f}%"
    if pnl >= levels["tp_pct"] * 0.7 and not pos.get("sold_2"):
        provider = Provider(); bal = await get_jetton_balance(provider, info["addr"])
        if bal > 0:
            part = int(bal * 0.5)
            if part > 0:
                ok = await execute_sell(symbol, part, info["addr"])
                if ok: pos["sold_2"] = True; action = f"💵 {symbol}: بيع 33% @ +{pnl*100:.1f}%"
    if md["price"] >= levels["tp"] or md["price"] <= pos.get("sl_price", levels["sl"]):
        provider = Provider(); bal = await get_jetton_balance(provider, info["addr"])
        if bal > 0:
            ok = await execute_sell(symbol, bal, info["addr"])
            if ok:
                profit = pos["ton_invested"] * pnl; state["total_pnl_ton"] += profit
                state["history"].append({"symbol": symbol, "term": term, "buy": pos["buy_price"], "sell": md["price"], "pnl_pct": pnl, "pnl_ton": profit, "t": time.time()})
                if pnl > 0: state["stats"]["wins"] += 1; state["stats"]["gross_profit"] += profit
                else: state["stats"]["losses"] += 1; state["stats"]["gross_loss"] += abs(profit)
                del state["positions"][symbol]; emoji = "💰" if pnl > 0 else "🔻"
                action = f"{emoji} إغلاق {symbol} | {pnl*100:+.1f}% | {profit:+.4f} TON"
    return action

async def handle_token(symbol, info, ton_balance):
    if not HALAL_WHITELIST.get(symbol, False) or info["addr"] == "EQ...": return None
    md = await get_market_data(info["addr"])
    if not md or md["price"] == 0: return None
    if symbol not in price_history: price_history[symbol] = []
    price_history[symbol].append({"t": time.time(), "price": md["price"], "volume": md["volume_1h"]})
    price_history[symbol] = price_history[symbol][-200:]
    regime = detect_market_regime(price_history[symbol])
    sig = score_signal(symbol, info["term"], md, price_history[symbol], regime)
    action = None; pos = state["positions"].get(symbol)
    if pos: action = await manage_position(symbol, info, md, pos)
    elif sig["score"] >= MIN_SCORE_TO_BUY and len(state["positions"]) < MAX_OPEN_POSITIONS:
        size = calculate_position_size(md, sig["score"], ton_balance, info["term"])
        if size > 0:
            ok = await execute_buy(symbol, size, info["addr"])
            if ok:
                levels = get_tp_sl_levels(info["term"], sig["score"], md["price"])
                state["positions"][symbol] = {"buy_price": md["price"], "ton_invested": size, "term": info["term"], "buy_time": time.time(), "score": sig["score"], "levels": levels, "sl_price": levels["sl"], "regime": regime}
                action = f"🟢 فتح {symbol} | {sig['score']} نقطة | {size} TON"
    return {"symbol": symbol, "term": info["term"], "price": md["price"], "score": sig["score"], "reasons": sig["reasons"], "regime": regime, "action": action, "in_position": symbol in state["positions"], "liquidity": md["liquidity"]}

def get_performance_stats():
    s = state["stats"]; total = s["wins"] + s["losses"]; wr = (s["wins"] / total * 100) if total > 0 else 0; pf = (s["gross_profit"] / s["gross_loss"]) if s["gross_loss"] > 0 else 0
    return {"win_rate": wr, "profit_factor": pf, "trades": total, "gross_profit": s["gross_profit"], "gross_loss": s["gross_loss"], "max_dd": s["max_drawdown"]}

async def main():
    load_state()
    await notify(f"🚀 <b>بوت التداول المؤسسي v4</b>\n💼 <code>{WALLET_ADDRESS[:20]}...</code>\n💰 رأس المال: ${STARTING_CAPITAL}\n📊 عملات مسموحة: {sum(1 for v in HALAL_WHITELIST.values() if v)}\n⚡ 8 مؤشرات + إدارة مخاطر")
    provider = Provider()
    while True:
        try:
            state["cycles"] += 1; cycle = state["cycles"]; ton_balance = await get_ton_balance(provider); total_balance = ton_balance
            if total_balance > state["stats"]["peak_balance"]: state["stats"]["peak_balance"] = total_balance
            dd = (state["stats"]["peak_balance"] - total_balance) / state["stats"]["peak_balance"] if state["stats"]["peak_balance"] > 0 else 0
            if dd > state["stats"]["max_drawdown"]: state["stats"]["max_drawdown"] = dd
            today = datetime.now().strftime("%Y-%m-%d")
            if state["daily_date"] != today: state["daily_date"] = today; state["daily_start"] = ton_balance
            if state["daily_start"] and state["daily_start"] > 0:
                daily_loss = (state["daily_start"] - ton_balance) / state["daily_start"]
                if daily_loss >= MAX_DAILY_LOSS_PCT:
                    await notify(f"🛑 <b>حماية يومية</b>\nخسارة {daily_loss*100:.1f}% - توقف اليوم")
                    save_state(); await asyncio.sleep(3600); continue
            week = datetime.now().strftime("%Y-W%W")
            if state["weekly_date"] != week: state["weekly_date"] = week; state["weekly_start"] = ton_balance
            if state["weekly_start"] and state["weekly_start"] > 0:
                weekly_loss = (state["weekly_start"] - ton_balance) / state["weekly_start"]
                if weekly_loss >= 0.30:
                    await notify(f"🛑 <b>حماية أسبوعية</b>\nخسارة {weekly_loss*100:.1f}% - توقف أسبوع")
                    save_state(); await asyncio.sleep(3600); continue
            results = []
            for symbol, info in TOKENS.items():
                r = await handle_token(symbol, info, ton_balance)
                if r: results.append(r)
            results.sort(key=lambda x: x["score"], reverse=True)
            if cycle % 5 == 0:
                stats = get_performance_stats(); report = [f"📊 <b>تقرير #{cycle//5}</b>"]
                report.append(f"💼 TON: {ton_balance:.3f} | PnL: {state['total_pnl_ton']:+.3f}")
                report.append(f"📈 صفقات: {stats['trades']} | ربح: {stats['win_rate']:.0f}% | PF: {stats['profit_factor']:.2f}")
                report.append(f"📉 Max DD: {stats['max_dd']*100:.1f}%")
                if state["positions"]:
                    report.append(f"\n📌 <b>مراكز ({len(state['positions'])}):</b>")
                    for sym, p in state["positions"].items():
                        cur = next((r for r in results if r["symbol"] == sym), None)
                        if cur:
                            pnl = (cur["price"] - p["buy_price"]) / p["buy_price"]; report.append(f"  • {sym}: {pnl*100:+.1f}%")
                report.append(f"\n🏆 <b>أقوى الإشارات:</b>")
                for r in results[:6]:
                    emoji = "🟢" if r["score"] >= 65 else "🟡" if r["score"] >= 40 else "⚪"
                    regime_ar = {"bullish": "صاعد", "bearish": "هابط", "ranging": "عرضي", "volatile": "متقلب", "neutral": "محايد", "unknown": "?"}[r["regime"]]
                    report.append(f"{emoji} <b>{r['symbol']}</b> [{r['score']}] ({regime_ar})")
                actions = [r["action"] for r in results if r["action"]]
                if actions: report.append("\n⚡ <b>تنفيذات:</b>"); report.extend(actions)
                await notify("\n".join(report))
            save_state(); print(f"[{cycle}] TON:{ton_balance:.2f} PnL:{state['total_pnl_ton']:+.3f} Pos:{len(state['positions'])}")
        except Exception as e: print(f"❌ خطأ: {e}"); await notify(f"⚠️ خطأ: {str(e)[:100]}")
        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
