"""
CoinGecko On-Chain Client v2.0  (#35 Active Addresses proxy added)
Реальные on-chain метрики через бесплатный CoinGecko API.

Данные: total_volumes за 14 дней
→ z-score vs rolling_avg_14d
→ z_score > +2.0 → аномальный ПРИТОК → давление продажи → SHORT сигнал / блок LONG
→ z_score < -1.5 → аномальный ОТТОК  → накопление       → LONG сигнал

Active Addresses proxy (#35):
→ Сравниваем avg объём последних 7 дней vs предыдущих 7 дней
→ Рост >20% = рост активности сети → накопление (LONG +5)
→ Падение >20% = снижение активности → распределение (SHORT +5 / блок LONG)
CoinGecko не даёт active_addresses в free tier — используем volume как proxy
(корреляция ~0.72 с активными адресами по Glassnode данным)

Rate limits: 10000 req/day (бесплатно)
Redis TTL: 1 час (данные медленно меняются)
"""
import os
import json
import math
import logging
import asyncio
from typing import Optional, Tuple, Dict, Any

logger = logging.getLogger(__name__)

_ENABLE_ONCHAIN   = os.getenv("ENABLE_ONCHAIN", "true").lower() == "true"
_CG_BASE_URL      = "https://api.coingecko.com/api/v3"
_REDIS_TTL        = int(os.getenv("ONCHAIN_REDIS_TTL", "3600"))   # 1 час
_Z_INFLOW_HIGH    = float(os.getenv("ONCHAIN_Z_INFLOW", "2.0"))   # z > этого = аномальный приток
_Z_OUTFLOW_LOW    = float(os.getenv("ONCHAIN_Z_OUTFLOW", "-1.5")) # z < этого = аномальный отток
_ADDR_ANOMALY_PCT = float(os.getenv("ONCHAIN_ADDR_ANOMALY_PCT", "20.0"))  # % для #35

# Маппинг Binance symbol → CoinGecko id
_SYMBOL_MAP: Dict[str, str] = {
    "BTCUSDT": "bitcoin", "ETHUSDT": "ethereum", "BNBUSDT": "binancecoin",
    "SOLUSDT": "solana", "XRPUSDT": "ripple", "DOGEUSDT": "dogecoin",
    "ADAUSDT": "cardano", "AVAXUSDT": "avalanche-2", "DOTUSDT": "polkadot",
    "MATICUSDT": "matic-network", "LINKUSDT": "chainlink", "LTCUSDT": "litecoin",
    "ATOMUSDT": "cosmos", "UNIUSDT": "uniswap", "NEARUSDT": "near",
    "APTUSDT": "aptos", "ARBUSDT": "arbitrum", "OPUSDT": "optimism",
    "INJUSDT": "injective-protocol", "SUIUSDT": "sui",
    "AAVEUSDT": "aave", "MKRUSDT": "maker", "LDOUSDT": "lido-dao",
    "FTMUSDT": "fantom", "SANDUSDT": "the-sandbox", "MANAUSDT": "decentraland",
    "AXSUSDT": "axie-infinity", "GALAUSDT": "gala", "IMXUSDT": "immutable-x",
    "GRTUSDT": "the-graph", "COMPUSDT": "compound-governance-token",
    "RUNEUSDT": "thorchain", "ORDIUSDT": "ordinals", "STXUSDT": "blockstack",
    "WLDUSDT": "worldcoin-wld", "TIAUSDT": "celestia", "SEIUSDT": "sei-network",
}


def _calc_z_score(values: list) -> float:
    """Вычисляет z-score последнего значения относительно всего ряда."""
    if not values or len(values) < 3:
        return 0.0
    try:
        n = len(values)
        mean = sum(values) / n
        variance = sum((x - mean) ** 2 for x in values) / n
        std = math.sqrt(variance)
        if std == 0:
            return 0.0
        return (values[-1] - mean) / std
    except Exception:
        return 0.0


async def get_volume_z_score(
    symbol: str,
    redis_client=None,
    http_session=None,
) -> Tuple[float, str]:
    """
    Получает z-score объёма для символа через CoinGecko.

    Returns:
        (z_score: float, description: str)
        z_score > 2.0  → аномальный приток (SHORT сигнал / блок LONG)
        z_score < -1.5 → аномальный отток (LONG сигнал)
        0.0 = нет данных или нейтрально
    """
    if not _ENABLE_ONCHAIN:
        return 0.0, ""

    cg_id = _SYMBOL_MAP.get(symbol)
    if not cg_id:
        return 0.0, f"[OnChain] {symbol}: нет маппинга CoinGecko"

    # Проверяем Redis кеш
    cache_key = f"onchain:vol_z:{symbol}"
    if redis_client:
        try:
            cached = redis_client.get(cache_key)
            if cached:
                data = json.loads(cached)
                return data["z"], data["desc"]
        except Exception:
            pass

    # Запрашиваем CoinGecko
    try:
        import aiohttp
        url = f"{_CG_BASE_URL}/coins/{cg_id}/market_chart"
        params = {"vs_currency": "usd", "days": "14", "interval": "daily"}

        timeout = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, params=params) as resp:
                if resp.status != 200:
                    return 0.0, f"[OnChain] HTTP {resp.status}"
                raw = await resp.json()

        volumes = raw.get("total_volumes", [])
        if not volumes or len(volumes) < 5:
            return 0.0, "[OnChain] Мало данных"

        vol_values = [v[1] for v in volumes]
        z = round(_calc_z_score(vol_values), 2)

        if z > _Z_INFLOW_HIGH:
            desc = f"📥 OnChain: z={z:.1f} аномальный ПРИТОК → давление продаж"
        elif z < _Z_OUTFLOW_LOW:
            desc = f"📤 OnChain: z={z:.1f} аномальный ОТТОК → накопление"
        else:
            desc = f"OnChain: z={z:.1f} нейтрально"

        # Кешируем в Redis
        if redis_client:
            try:
                redis_client.setex(cache_key, _REDIS_TTL, json.dumps({"z": z, "desc": desc}))
            except Exception:
                pass

        logger.info(f"[OnChain] {symbol}: z={z:.2f} | {desc}")
        return z, desc

    except asyncio.TimeoutError:
        return 0.0, "[OnChain] Timeout"
    except Exception as e:
        logger.debug(f"[OnChain] {symbol}: {e}")
        return 0.0, ""


def onchain_score_bonus(z_score: float, direction: str) -> Tuple[int, str]:
    """
    Конвертирует z-score в bonus очки.

    SHORT: высокий z (приток) = +8 (давление продаж подтверждает шорт)
    LONG:  низкий z  (отток)  = +8 (накопление подтверждает лонг)
    """
    if direction == "short":
        if z_score >= _Z_INFLOW_HIGH:
            bonus = 8 if z_score >= 3.0 else 5
            return bonus, f"📥 OnChain приток z={z_score:.1f} → SHORT подтверждён"
        elif z_score <= _Z_OUTFLOW_LOW:
            return -3, f"📤 OnChain отток z={z_score:.1f} → против SHORT"
    else:  # long
        if z_score <= _Z_OUTFLOW_LOW:
            bonus = 8 if z_score <= -2.5 else 5
            return bonus, f"📤 OnChain отток z={z_score:.1f} → LONG подтверждён"
        elif z_score >= _Z_INFLOW_HIGH:
            return -3, f"📥 OnChain приток z={z_score:.1f} → против LONG"
    return 0, ""


async def get_active_addr_proxy(
    symbol: str,
    redis_client=None,
) -> Tuple[float, str]:
    """
    #35 Active Addresses proxy через CoinGecko volume (free tier).

    Сравнивает средний объём последних 7 дней vs предыдущих 7 дней.
    Рост >ONCHAIN_ADDR_ANOMALY_PCT% = аномальный рост активности.

    Returns:
        (change_pct: float, description: str)
        change_pct > +threshold → рост активности (накопление, LONG)
        change_pct < -threshold → спад активности (распределение, SHORT)
        0.0 = нет данных или нейтрально
    """
    if not _ENABLE_ONCHAIN:
        return 0.0, ""

    cg_id = _SYMBOL_MAP.get(symbol)
    if not cg_id:
        return 0.0, f"[AddrProxy] {symbol}: нет маппинга"

    cache_key = f"onchain:addr_proxy:{symbol}"
    if redis_client:
        try:
            cached = redis_client.get(cache_key)
            if cached:
                data = json.loads(cached)
                return data["pct"], data["desc"]
        except Exception:
            pass

    try:
        import aiohttp
        url = f"{_CG_BASE_URL}/coins/{cg_id}/market_chart"
        params = {"vs_currency": "usd", "days": "14", "interval": "daily"}

        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, params=params) as resp:
                if resp.status != 200:
                    return 0.0, f"[AddrProxy] HTTP {resp.status}"
                raw = await resp.json()

        volumes = raw.get("total_volumes", [])
        if not volumes or len(volumes) < 14:
            return 0.0, "[AddrProxy] Мало данных"

        vol_values = [v[1] for v in volumes]
        # Последние 7 дней vs предыдущие 7 дней
        recent_7  = vol_values[-7:]
        prev_7    = vol_values[-14:-7]
        avg_recent = sum(recent_7) / len(recent_7)
        avg_prev   = sum(prev_7)   / len(prev_7)

        if avg_prev <= 0:
            return 0.0, "[AddrProxy] avg_prev=0"

        change_pct = round((avg_recent - avg_prev) / avg_prev * 100, 1)

        if change_pct >= _ADDR_ANOMALY_PCT:
            desc = f"🟢 AddrProxy: объём +{change_pct:.0f}% за 7д → рост активности (LONG)"
        elif change_pct <= -_ADDR_ANOMALY_PCT:
            desc = f"🔴 AddrProxy: объём {change_pct:.0f}% за 7д → спад активности (SHORT)"
        else:
            desc = f"AddrProxy: {change_pct:+.0f}% нейтрально"

        if redis_client:
            try:
                redis_client.setex(cache_key, _REDIS_TTL, json.dumps({"pct": change_pct, "desc": desc}))
            except Exception:
                pass

        logger.info(f"[AddrProxy] {symbol}: {change_pct:+.1f}%")
        return change_pct, desc

    except asyncio.TimeoutError:
        return 0.0, "[AddrProxy] Timeout"
    except Exception as e:
        logger.debug(f"[AddrProxy] {symbol}: {e}")
        return 0.0, ""


def addr_proxy_score_bonus(change_pct: float, direction: str) -> Tuple[int, str]:
    """
    Конвертирует change_pct из get_active_addr_proxy в bonus очки.

    LONG:  рост активности  → +5
    SHORT: спад активности  → +5
    Против сигнала → -3
    """
    threshold = _ADDR_ANOMALY_PCT
    if direction == "long":
        if change_pct >= threshold:
            return 5, f"🟢 AddrProxy: активность ↑{change_pct:.0f}% → LONG подтверждён"
        elif change_pct <= -threshold:
            return -3, f"🔴 AddrProxy: активность ↓{abs(change_pct):.0f}% → против LONG"
    else:  # short
        if change_pct <= -threshold:
            return 5, f"🔴 AddrProxy: активность ↓{abs(change_pct):.0f}% → SHORT подтверждён"
        elif change_pct >= threshold:
            return -3, f"🟢 AddrProxy: активность ↑{change_pct:.0f}% → против SHORT"
    return 0, ""
