"""
Netflow Analyzer Long v1.0
Exchange Netflow из CoinGlass → LONG сигнал накопления.

Логика:
  Отрицательный netflow (outflow) = монеты уходят с бирж в cold wallets = институциональное накопление.
  Накопление → уменьшение supply на биржах → price pressure вверх.

Данные: CoinGlass /public/v2/indicator/exchange_flow
"""

from __future__ import annotations
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger("aegis.netflow_analyzer")

_COINGLASS_KEY = os.getenv("COINGLASS_API_KEY", "")


class NetflowAnalyzerLong:
    """
    Анализ Exchange Netflow (CoinGlass) для LONG сигналов.
    Outflow с бирж = накопление = бычий сигнал.
    """

    def __init__(self, coinglass_client=None):
        self._client = coinglass_client
        self._cache: Dict[str, Dict] = {}
        self._cache_ttl = 3600  # 1 час — netflow меняется медленно
        self._last_fetch: Dict[str, float] = {}

    async def analyze(self, symbol: str) -> Dict:
        import time

        # Кэш: netflow дорогой запрос, обновляем раз в час
        now = time.time()
        if symbol in self._cache and (now - self._last_fetch.get(symbol, 0)) < self._cache_ttl:
            return self._cache[symbol]

        result = await self._fetch(symbol)
        self._cache[symbol] = result
        self._last_fetch[symbol] = now
        return result

    async def _fetch(self, symbol: str) -> Dict:
        reasons = []
        score   = 40.0  # neutral baseline

        if not _COINGLASS_KEY:
            return {"score": score, "reasons": ["Netflow: COINGLASS_API_KEY не задан"], "metadata": {}}

        if self._client is None:
            try:
                from shared.api.coinglass_client import get_coinglass_client
                self._client = get_coinglass_client()
            except ImportError:
                try:
                    import sys, os as _os
                    sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), "..", "..", "..", "shared"))
                    from api.coinglass_client import get_coinglass_client
                    self._client = get_coinglass_client()
                except Exception:
                    return {"score": score, "reasons": ["Netflow: CoinGlass client недоступен"], "metadata": {}}

        try:
            # Базовый тикер без USDT (BTC, ETH, etc.)
            base = symbol.replace("USDT", "").replace("PERP", "").replace("-", "")
            data = await self._client.get_exchange_netflow(base, interval="h8")
        except Exception as e:
            logger.warning(f"[Netflow] {symbol}: ошибка запроса — {e}")
            return {"score": score, "reasons": [f"Netflow: ошибка API"], "metadata": {}}

        if not data:
            return {"score": score, "reasons": ["Netflow: нет данных"], "metadata": {}}

        net_flow = data.get("net_flow", 0)
        signal   = data.get("signal", "neutral")
        cg_score = data.get("score", 40)

        if signal == "accumulation":
            score = cg_score
            if cg_score >= 75:
                reasons.append(f"📦 STRONG OUTFLOW: монеты уходят с бирж — институциональное накопление")
            elif cg_score >= 55:
                reasons.append(f"📦 Outflow ({net_flow:+.0f}) — умеренное накопление")
            else:
                reasons.append(f"Netflow нейтральный с outflow уклоном")
        elif signal == "distribution":
            score = max(10, 40 - abs(net_flow) / 5_000_000)
            reasons.append(f"⚠️ Inflow на биржи ({net_flow:+.0f}) — осторожно для LONG")
        else:
            score = 40
            reasons.append("Netflow нейтральный")

        return {
            "score":   round(min(score, 100), 1),
            "reasons": reasons,
            "metadata": {
                "net_flow":  net_flow,
                "inflow":    data.get("inflow", 0),
                "outflow":   data.get("outflow", 0),
                "signal":    signal,
            },
        }
