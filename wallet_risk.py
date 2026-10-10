import os
import json
import time
import asyncio
from decimal import Decimal, InvalidOperation
from pathlib import Path
from datetime import datetime, timezone

import httpx

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
CONFIG_FILE = DATA_DIR / "wallet_risk.json"

TON_API = os.getenv("TON_API_BASE", "https://toncenter.com/api/v2")
TON_API_KEY = os.getenv("TONCENTER_API_KEY", "")
TONCENTER_V3 = "https://toncenter.com/api/v3"
DEXPAPRIKA_API_KEY = os.getenv("DEXPAPRIKA_API_KEY", "").strip()

DEFAULTS = {
    "wallet_address": "",
    "manual_balance_usd": "6.00",
    "use_manual_balance": True,
    "trade_allocation_pct": "10",
    "risk_per_trade_pct": "1",
    "daily_loss_limit_pct": "3",
    "reserve_ton": "0.35",
    "paused": True,
    "daily_start_balance_usd": "6.00",
    "daily_loss_usd": "0",
    "daily_date": "",
    "last_balance_check": 0,
}


def load_config():
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text())
            return {**DEFAULTS, **saved}
        except (OSError, json.JSONDecodeError):
            pass
    save_config(DEFAULTS.copy())
    return DEFAULTS.copy()


def save_config(config):
    temp = CONFIG_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(config, indent=2))
    temp.replace(CONFIG_FILE)


config = load_config()


def dec(value, default="0"):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else Decimal(default)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def set_manual_balance(amount):
    amount = dec(amount, "-1")
    if amount < 0:
        raise ValueError("الرصيد يجب أن يكون صفرًا أو أكثر")
    config["manual_balance_usd"] = str(amount)
    config["use_manual_balance"] = True
    save_config(config)
    return amount


def set_wallet(address):
    address = str(address).strip()
    if not address or len(address) < 20:
        raise ValueError("عنوان المحفظة غير صالح")
    config["wallet_address"] = address
    config["use_manual_balance"] = False
    save_config(config)


async def get_ton_balance():
    address = config.get("wallet_address", "").strip()
    if not address:
        raise ValueError("حدد عنوان المحفظة العام أولًا")
    headers = {}
    if TON_API_KEY:
        headers["X-API-Key"] = TON_API_KEY
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{TON_API}/getAddressBalance",
            params={"address": address},
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()
    if not data.get("ok") or "result" not in data:
        raise RuntimeError("تعذر التحقق من رصيد المحفظة")
    nano_ton = dec(data["result"], "-1")
    if nano_ton < 0:
        raise RuntimeError("استجابة الرصيد غير صالحة")
    balance = nano_ton / Decimal("1000000000")
    config["last_balance_check"] = int(time.time())
    save_config(config)
    return balance


async def get_ton_usd_price():
    url = "https://api.dexscreener.com/latest/dex/search"
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url, params={"q": "TON USDT"})
        response.raise_for_status()
        data = response.json()
    pairs = data.get("pairs") or []
    candidates = []
    for pair in pairs:
        if pair.get("chainId") != "ton":
            continue
        base = pair.get("baseToken") or {}
        quote = pair.get("quoteToken") or {}
        if str(base.get("symbol", "")).upper() != "TON":
            continue
        if str(quote.get("symbol", "")).upper() not in ("USDT", "USDC"):
            continue
        price = dec(pair.get("priceUsd"), "0")
        liquidity = dec((pair.get("liquidity") or {}).get("usd"), "0")
        if price > 0 and liquidity > 0:
            candidates.append((liquidity, price))
    if not candidates:
        raise RuntimeError("لم يُعثر على سعر TON/USD موثوق")
    candidates.sort(reverse=True)
    return candidates[0][1]


async def get_portfolio_usd():
    if config.get("use_manual_balance", True):
        return dec(config["manual_balance_usd"])
    ton = await get_ton_balance()
    price = await get_ton_usd_price()
    return ton * price


def calculate_position(balance_usd, entry, stop, estimated_cost_pct=0):
    balance = dec(balance_usd)
    entry = dec(entry)
    stop = dec(stop)
    costs = dec(estimated_cost_pct)
    if balance <= 0 or entry <= 0 or stop <= 0:
        raise ValueError("الرصيد والأسعار يجب أن تكون موجبة")
    if stop >= entry:
        raise ValueError("يجب أن يكون وقف الخسارة أقل من الدخول")
    if costs < 0 or costs >= 100:
        raise ValueError("نسبة التكاليف غير صالحة")
    allocation_pct = dec(config["trade_allocation_pct"])
    risk_pct = dec(config["risk_per_trade_pct"])
    allocation = balance * allocation_pct / Decimal("100")
    max_loss = balance * risk_pct / Decimal("100")
    price_loss_pct = (entry - stop) / entry
    total_loss_fraction = price_loss_pct + costs / Decimal("100")
    if total_loss_fraction <= 0:
        raise ValueError("تعذر حساب المخاطرة")
    risk_limited_size = max_loss / total_loss_fraction
    position = min(allocation, risk_limited_size, balance)
    return {
        "balance_usd": float(balance),
        "position_usd": round(float(position), 4),
        "max_planned_loss_usd": round(float(position * total_loss_fraction), 4),
        "allocation_limit_usd": round(float(allocation), 4),
        "estimated_cost_pct": float(costs),
    }


def daily_loss_limit_reached(balance_usd):
    balance = dec(balance_usd)
    start = dec(config["daily_start_balance_usd"])
    loss = dec(config["daily_loss_usd"])
    limit_pct = dec(config["daily_loss_limit_pct"])
    if start <= 0:
        return True
    return loss >= start * limit_pct / Decimal("100")


def pause():
    config["paused"] = True
    save_config(config)


def resume():
    config["paused"] = False
    save_config(config)


def status():
    return {
        "wallet_address_set": bool(config.get("wallet_address")),
        "manual_balance_usd": config["manual_balance_usd"],
        "use_manual_balance": config["use_manual_balance"],
        "trade_allocation_pct": config["trade_allocation_pct"],
        "risk_per_trade_pct": config["risk_per_trade_pct"],
        "daily_loss_limit_pct": config["daily_loss_limit_pct"],
        "paused": config["paused"],
        "last_balance_check": config["last_balance_check"],
        "mode": "READ_ONLY_PAPER_TRADING",
    }


async def find_pool_for_token(token_address):
    url = "https://api.dexpaprika.com/networks/ton/pools/search"
    params = {
        "token_address": token_address,
        "order_by": "volume_usd_24h",
        "sort": "desc",
        "limit": 5,
    }
    headers = {}
    if DEXPAPRIKA_API_KEY:
        headers["Authorization"] = DEXPAPRIKA_API_KEY
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, params=params, headers=headers)
            r.raise_for_status()
            data = r.json()
        results = data.get("results") or []
        if not results:
            return None
        return results[0].get("id")
    except Exception as e:
        print(f"dexpaprika pool fail: {e}")
        return None


async def get_ohlcv(pool_address, interval="1m", limit=100):
    url = f"https://api.dexpaprika.com/networks/ton/pools/{pool_address}/ohlcv"
    params = {"interval": interval, "limit": limit}
    headers = {}
    if DEXPAPRIKA_API_KEY:
        headers["Authorization"] = DEXPAPRIKA_API_KEY
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, params=params, headers=headers)
            r.raise_for_status()
            data = r.json()
        if not isinstance(data, list):
            return []
        candles = []
        for c in data:
            try:
                t_str = c.get("time_open", "")
                t = int(datetime.fromisoformat(t_str.replace("Z", "+00:00")).timestamp())
                candles.append({
                    "t": t,
                    "o": float(c.get("open", 0) or 0),
                    "h": float(c.get("high", 0) or 0),
                    "l": float(c.get("low", 0) or 0),
                    "c": float(c.get("close", 0) or 0),
                })
            except (ValueError, TypeError):
                continue
        return candles
    except Exception as e:
        print(f"dexpaprika ohlcv fail: {e}")
        return []


async def get_recent_swaps(wallet_address, limit=20):
    url = f"{TONCENTER_V3}/actions"
    params = {
        "account": wallet_address,
        "action_type": "jetton_swap",
        "limit": limit,
        "sort": "desc",
    }
    headers = {}
    if TON_API_KEY:
        headers["X-API-Key"] = TON_API_KEY
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, params=params, headers=headers)
            r.raise_for_status()
            data = r.json()
        if isinstance(data, dict):
            return data.get("actions") or data.get("result") or []
        return []
    except Exception as e:
        print(f"toncenter v3 swaps fail: {e}")
        return []


def find_swap_for_token(swaps, token_address, since_time=0):
    token_norm = token_address.split("_")[0].strip().lower()
    for action in swaps:
        swap = action.get("jetton_swap") or action.get("jettonSwap") or {}
        if not swap:
            continue
        in_addr = ((swap.get("jetton_master_in") or {}).get("address") or "")
        out_addr = ((swap.get("jetton_master_out") or {}).get("address") or "")
        in_addr = in_addr.split("_")[0].strip().lower()
        out_addr = out_addr.split("_")[0].strip().lower()
        if token_norm not in (in_addr, out_addr):
            continue
        t = action.get("start_utime", 0) or action.get("end_utime", 0)
        try:
            t = int(t)
        except (ValueError, TypeError):
            t = 0
        if t >= since_time:
            return action
    return None


async def get_actual_swap_price(token_address, swap_action):
    ts = swap_action.get("start_utime", 0) or swap_action.get("end_utime", 0)
    try:
        ts = int(ts)
    except (ValueError, TypeError):
        return None
    if not ts:
        return None
    pool = await find_pool_for_token(token_address)
    if not pool:
        return None
    candles = await get_ohlcv(pool, interval="1m", limit=200)
    if not candles:
        candles = await get_ohlcv(pool, interval="1h", limit=100)
    if not candles:
        return None
    closest = min(candles, key=lambda c: abs(c["t"] - ts))
    return closest["c"]
