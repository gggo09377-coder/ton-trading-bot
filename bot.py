import asyncio
import os
import time
import json
from datetime import datetime
from dotenv import load_dotenv
from tonsdk.contract.wallet import Wallets, WalletVersionEnum
from tonsdk.utils import Address, bytes_to_b64str
from dedust import Asset, Factory, PoolType, SwapParams, VaultJetton, VaultNative
from dedust.api import Provider
import httpx

load_dotenv()

# ===== الإعدادات =====
MNEMONIC = os.getenv("TON_MNEMONIC", "").split()
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
STARTING_CAPITAL = float(os.getenv("STARTING_CAPITAL", "6.0"))
MAX_TON_PER_TRADE = float(os.getenv("MAX_TON_PER_TRADE", "1.0"))
MIN_TON_RESERVE = float(os.getenv("MIN_TON_RESERVE", "1.5"))
SHORT_TP = float(os.getenv("SHORT_TP", "0.20"))
SHORT_SL = float(os.getenv("SHORT_SL", "0.08"))
MEDIUM_TP = float(os.getenv("MEDIUM_TP", "0.50"))
MEDIUM_SL = float(os.getenv("MEDIUM_SL", "0.15"))
LONG_TP = float(os.getenv("LONG_TP", "1.50"))
LONG_SL = float(os.getenv("LONG_SL", "0.30"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "180"))

STATE_FILE = "state.json"

# ===== قائمة العملات =====
# ⚠️ مهم جداً: حط عنوان Jetton الحقيقي لكل عملة مكان EQ...
# تقدر تاخذ العناوين من موقع TonViewer.com
TOKENS = {
    # مدى قريب - تقلب عالي
    "XROCK":  {"addr": "EQ...", "term": "short", "buy_price": None, "ton_invested": 0},
    "STORM":  {"addr": "EQ...", "term": "short", "buy_price": None, "ton_invested": 0},
    "UTYA":   {"addr": "EQ...", "term": "short", "buy_price": None, "ton_invested": 0},
    "LAMBO":  {"addr": "EQ...", "term": "short", "buy_price": None, "ton_invested": 0},
    "RAFF":   {"addr": "EQ...", "term": "short", "buy_price": None, "ton_invested": 0},
    "REDO":   {"addr": "EQ...", "term": "short", "buy_price": None, "ton_invested": 0},
    "ATF":    {"addr": "EQ...", "term": "short", "buy_price": None, "ton_invested": 0},
    "MY":     {"addr": "EQ...", "term": "short", "buy_price": None, "ton_invested": 0},
    "PX":     {"addr": "EQ...", "term": "short", "buy_price": None, "ton_invested": 0},
    "durev":  {"addr": "EQ...", "term": "short", "buy_price": None, "ton_invested": 0},
    "TAC":    {"addr": "EQ...", "term": "short", "buy_price": None, "ton_invested": 0},

    # مدى متوسط
    "GRAM":   {"addr": "EQ...", "term": "medium", "buy_price": None, "ton_invested": 0},
    "STON":   {"addr": "EQ...", "term": "medium", "buy_price": None, "ton_invested": 0},
    "NOT":    {"addr": "EQ...", "term": "medium", "buy_price": None, "ton_invested": 0},

    # مدى بعيد
    "cbBTC":  {"addr": "EQ...", "term": "long", "buy_price": None, "ton_invested": 0},
    "WETH":   {"addr": "EQ...", "term": "long", "buy_price": None, "ton_invested": 0},
}

# ===== المحفظة =====
mnemonics, pub_k, priv_k, wallet = Wallets.from_mnemonics(
    mnemonics=MNEMONIC, version=WalletVersionEnum.v4r2, workchain=0
)
WALLET_ADDRESS = wallet.address.to_string(is_user_friendly=True, is_bounceable=True)

# ===== الحالة =====
state = {
    "positions": {},
    "history": [],
    "total_pnl_ton": 0.0,
    "cycles": 0
}
price_history = {}

def load_state():
    global state, price_history
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                d = json.load(f)
                state = d.get("state", state)
                price_history = d.get("price_history", {})
        except:
            pass

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"state": state, "price_history": price_history}, f, indent=2)
    except:
        pass

# ===== تيليكرام =====
async def notify(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print(msg)
        return
    try:
        async with httpx.AsyncClient() as c:
            await c.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
                timeout=10
            )
    except Exception as e:
        print(f"إشعار فشل: {e}")

# ===== جلب السعر =====
async def get_price(token_addr):
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"https://api.dexscreener.com/latest/dex/search?q={token_addr}",
                timeout=10
            )
            data = r.json()
            if "pairs" in data and data["pairs"]:
                pairs = sorted(
                    data["pairs"],
                    key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0,
                    reverse=True
                )
                return float(pairs[0].get("priceUsd", 0))
    except:
        pass
    return None

# ===== جلب رصيد TON =====
async def get_ton_balance(provider):
    try:
        r = await provider.runGetMethod(address=wallet.address, method="get_wallet_data")
        return int(r[0]["value"]) / 1e9
    except:
        return 0.0

# ===== جلب رصيد Jetton =====
async def get_jetton_balance(provider, jetton_addr):
    try:
        jw = await provider.runGetMethod(
            address=Address(jetton_addr),
            method="get_wallet_address",
            stack=[["slice", wallet.address.to_string()]]
        )
        wallet_addr = jw[0]["value"]
        bal = await provider.runGetMethod(
            address=Address(wallet_addr),
            method="get_wallet_data"
        )
        return int(bal[0]["value"])
    except:
        return 0

# ===== تحليل فني =====
def analyze(symbol, term):
    hist = price_history.get(symbol, [])
    if len(hist) < 5:
        return {"signal": "hold", "reason": "بيانات غير كافية", "rsi": 50}
    
    prices = [h["price"] for h in hist[-30:]]
    current = prices[-1]
    
    ma5 = sum(prices[-5:]) / 5
    ma10 = sum(prices[-10:]) / 10 if len(prices) >= 10 else ma5
    ma20 = sum(prices[-20:]) / 20 if len(prices) >= 20 else ma5
    
    gains = [max(prices[i] - prices[i-1], 0) for i in range(1, len(prices))]
    losses = [max(prices[i-1] - prices[i], 0) for i in range(1, len(prices))]
    ag = sum(gains) / len(gains) if gains else 0
    al = sum(losses) / len(losses) if losses else 0
    rsi = 100 - (100 / (1 + ag / al)) if al > 0 else 100
    
    mom = (current - prices[0]) / prices[0] if prices[0] > 0 else 0
    
    signal = "hold"
    reason = f"RSI={rsi:.0f} MOM={mom*100:+.1f}%"
    
    if term == "short":
        if mom > 0.05 and rsi < 75:
            signal = "buy"
            reason = f"زخم صاعد {mom*100:+.1f}%"
        elif rsi > 80:
            signal = "sell"
            reason = f"ذروة شراء RSI={rsi:.0f}"
    elif term == "medium":
        if ma5 > ma10 > ma20 and rsi < 70:
            signal = "buy"
            reason = f"اتجاه صاعد MA"
        elif ma5 < ma10 and rsi > 65:
            signal = "sell"
            reason = f"انعكاس اتجاه"
    elif term == "long":
        if rsi < 35:
            signal = "buy"
            reason = f"فرصة تراكم RSI={rsi:.0f}"
        elif rsi > 75:
            signal = "sell"
            reason = f"تصريف RSI={rsi:.0f}"
    
    return {"signal": signal, "reason": reason, "rsi": rsi, "momentum": mom}

# ===== تنفيذ الشراء =====
async def execute_buy(symbol, ton_amount, token_addr):
    try:
        provider = Provider()
        TON = Asset.native()
        TOKEN = Asset.jetton(Address(token_addr))
        pool = await Factory.get_pool(pool_type=PoolType.VOLATILE, assets=[TON, TOKEN], provider=provider)
        swap_params = SwapParams(deadline=int(time.time() + 300), recipient_address=wallet.address)
        payload = VaultNative.create_swap_payload(amount=int(ton_amount * 1e9), pool_address=pool.address, swap_params=swap_params, limit=0)
        seqno = await provider.runGetMethod(address=wallet.address, method="seqno")
        query = wallet.create_transfer_message(to_addr=Address(pool.address), amount=int((ton_amount + 0.25) * 1e9), seqno=seqno[0]["value"], payload=payload)
        boc = bytes_to_b64str(query["message"].to_boc(False))
        await provider.sendBoc(boc)
        await notify(f"🟢 <b>شراء {symbol}</b>\nTON: {ton_amount:.3f}")
        return True
    except Exception as e:
        await notify(f"❌ فشل شراء {symbol}: {str(e)[:80]}")
        return False

# ===== تنفيذ البيع =====
async def execute_sell(symbol, amount_nano, token_addr):
    try:
        provider = Provider()
        TON = Asset.native()
        TOKEN = Asset.jetton(Address(token_addr))
        pool = await Factory.get_pool(pool_type=PoolType.VOLATILE, assets=[TON, TOKEN], provider=provider)
        swap_params = SwapParams(deadline=int(time.time() + 300), recipient_address=wallet.address)
        payload = VaultJetton.create_swap_payload(amount=amount_nano, pool_address=pool.address, swap_params=swap_params, limit=0)
        seqno = await provider.runGetMethod(address=wallet.address, method="seqno")
        query = wallet.create_transfer_message(to_addr=Address(pool.address), amount=int(0.25 * 1e9), seqno=seqno[0]["value"], payload=payload)
        boc = bytes_to_b64str(query["message"].to_boc(False))
        await provider.sendBoc(boc)
        await notify(f"🔴 <b>بيع {symbol}</b>")
        return True
    except Exception as e:
        await notify(f"❌ فشل بيع {symbol}: {str(e)[:80]}")
        return False

# ===== إدارة عملة واحدة =====
async def handle_token(symbol, info, ton_balance):
    if info["addr"] == "EQ...":
        return None
    price = await get_price(info["addr"])
    if not price:
        return None
    
    if symbol not in price_history:
        price_history[symbol] = []
    price_history[symbol].append({"t": time.time(), "price": price})
    price_history[symbol] = price_history[symbol][-100:]
    
    analysis = analyze(symbol, info["term"])
    term = info["term"]
    if term == "short": tp, sl = SHORT_TP, SHORT_SL
    elif term == "medium": tp, sl = MEDIUM_TP, MEDIUM_SL
    else: tp, sl = LONG_TP, LONG_SL
    
    action = None
    if info["buy_price"] is None and analysis["signal"] == "buy":
        if ton_balance - MIN_TON_RESERVE >= MAX_TON_PER_TRADE:
            ok = await execute_buy(symbol, MAX_TON_PER_TRADE, info["addr"])
            if ok:
                info["buy_price"] = price
                info["ton_invested"] = MAX_TON_PER_TRADE
                state["positions"][symbol] = {"buy_price": price, "ton_invested": MAX_TON_PER_TRADE, "term": term, "buy_time": time.time()}
                action = f"🟢 شراء {symbol} @ {price:.6f}"
    elif info["buy_price"] is not None:
        pnl = (price - info["buy_price"]) / info["buy_price"]
        if pnl >= tp or pnl <= -sl:
            provider = Provider()
            bal = await get_jetton_balance(provider, info["addr"])
            if bal > 0:
                ok = await execute_sell(symbol, bal, info["addr"])
                if ok:
                    profit_ton = info["ton_invested"] * pnl
                    state["total_pnl_ton"] += profit_ton
                    state["history"].append({"symbol": symbol, "term": term, "buy": info["buy_price"], "sell": price, "pnl_pct": pnl, "pnl_ton": profit_ton, "t": time.time()})
                    if symbol in state["positions"]: del state["positions"][symbol]
                    info["buy_price"] = None
                    info["ton_invested"] = 0
                    emoji = "💰" if pnl > 0 else "🔻"
                    action = f"{emoji} بيع {symbol} @ {price:.6f} | {pnl*100:+.1f}%"
    return {"symbol": symbol, "term": term, "price": price, "analysis": analysis, "action": action, "in_position": info["buy_price"] is not None}

# ===== الحلقة الرئيسية =====
async def main():
    load_state()
    await notify(f"🚀 <b>بوت التداول الآلي - TON</b>\n💼 المحفظة: <code>{WALLET_ADDRESS[:20]}...</code>\n💰 رأس المال: ${STARTING_CAPITAL}\n📊 {len(TOKENS)} عملة")
    print(f"🚀 بدأ البوت | {WALLET_ADDRESS}")
    provider = Provider()
    
    while True:
        try:
            state["cycles"] += 1
            cycle = state["cycles"]
            ton_balance = await get_ton_balance(provider)
            results = []
            for symbol, info in TOKENS.items():
                r = await handle_token(symbol, info, ton_balance)
                if r: results.append(r)
            
            if cycle % 5 == 0:
                report = [f"📊 <b>تقرير #{cycle//5}</b> | {datetime.now().strftime('%H:%M')}"]
                report.append(f"💼 رصيد TON: {ton_balance:.3f}")
                report.append(f"💵 PnL: {state['total_pnl_ton']:+.3f} TON\n")
                if state["positions"]:
                    report.append("📌 <b>مراكز مفتوحة:</b>")
                    for sym, pos in state["positions"].items(): report.append(f"  • {sym} ({pos['term']})")
                    report.append("")
                report.append("📡 <b>إشارات:</b>")
                for r in results[:8]:
                    emoji = {"buy": "🟢", "sell": "🔴", "hold": "⚪"}[r["analysis"]["signal"]]
                    report.append(f"{emoji} <b>{r['symbol']}</b> ({r['term']}): ${r['price']:.6f} | {r['analysis']['reason'][:25]}")
                actions = [r["action"] for r in results if r["action"]]
                if actions:
                    report.append("\n⚡ <b>تنفيذات:</b>")
                    report.extend(actions)
                await notify("\n".join(report))
            save_state()
            print(f"[{cycle}] TON:{ton_balance:.2f} PnL:{state['total_pnl_ton']:+.3f}")
        except Exception as e:
            print(f"❌ خطأ الدورة: {e}")
            await notify(f"⚠️ خطأ: {str(e)[:100]}")
        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
