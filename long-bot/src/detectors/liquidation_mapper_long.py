"""
Liquidation Mapper Long v1.0
Анализ зон потенциальных SHORT-сквизов для LONG бота.

Для LONG нас интересуют SHORT liquidation clusters:
  цена растёт → шорты ликвидируются → цена ускоряется.

Метрики:
  - Short ratio (= 100 - ls_ratio): высокий = толпа шортит = squeeze fuel
  - Funding rate < 0: шорты платят лонгам → накапливается давление squeeze
  - OI рост при падении цены = новые шорты на дне = prime squeeze target
  - price_4d < -15%: рынок давили → много шортов открыто
"""
from __future__ import annotations
import logging
from typing import Any, Dict, List

logger = logging.getLogger("aegis.liq_mapper_long")


class LiquidationMapperLong:
    """
    Картирует потенциальные зоны ликвидации шортов (short squeeze).
    SHORT liquidation clusters = магниты для LONG — цена притягивается к ним снизу вверх.
    """

    async def analyze(self, symbol: str, market_data: Any) -> Dict:
        reasons: List[str] = []
        score = 0.0

        try:
            funding    = getattr(market_data, "funding_rate", 0) or 0
            ls_ratio   = getattr(market_data, "long_short_ratio", 50) or 50
            price_4d   = getattr(market_data, "price_change_4d", 0) or 0
            price_1h   = getattr(market_data, "price_change_1h", 0) or 0
            oi_4d      = getattr(market_data, "oi_change_4d", 0) or 0
            short_ratio = 100 - ls_ratio  # % открытых шортов

            # ── Зона ликвидации шортов ───────────────────────────────────
            # Высокий short ratio + dump = шорты на дне = potential squeeze
            short_liq_score = 0.0

            if short_ratio > 65 and price_4d < -15:
                short_liq_score = 80
                reasons.append(
                    f"💥 SHORT SQUEEZE ZONE: {short_ratio:.0f}% шортов после -{abs(price_4d):.1f}%"
                )
            elif short_ratio > 60 and price_4d < -8:
                short_liq_score = 60
                reasons.append(
                    f"⚠️ Short exposure high: {short_ratio:.0f}% после -{abs(price_4d):.1f}%"
                )
            elif short_ratio > 55:
                short_liq_score = 35
                reasons.append(f"Short bias: {short_ratio:.0f}% — толпа против рынка")

            # Отрицательный funding + шортовый перекос = шорты переплачивают → сквиз нарастает
            if funding < -0.05 and short_ratio > 55:
                short_liq_score = min(short_liq_score + 20, 100)
                reasons.append(
                    f"Funding {funding:.3f}% (neg) + short bias — short squeeze давление"
                )

            # OI растёт при падении = новые шорты открываются = будущие жертвы сквиза
            if oi_4d > 20 and price_4d < -10:
                short_liq_score = min(short_liq_score + 15, 100)
                reasons.append(
                    f"OI +{oi_4d:.1f}% при цене -{abs(price_4d):.1f}% — новые шорты на дне"
                )

            score = short_liq_score

            # Негативный фактор: уже растём быстро — часть сквизов прошла
            if price_1h > 3:
                score *= 0.8
                reasons.append("⬆️ Рост уже идёт — часть short squeeze прошла")

        except Exception as e:
            logger.warning(f"liq_mapper_long error {symbol}: {e}")
            score = 20.0

        return {
            "score":   round(min(score, 100), 1),
            "reasons": reasons,
            "metadata": {
                "ls_ratio":   getattr(market_data, "long_short_ratio", 50),
                "short_ratio": 100 - (getattr(market_data, "long_short_ratio", 50) or 50),
                "price_4d":   getattr(market_data, "price_change_4d", 0),
                "funding":    getattr(market_data, "funding_rate", 0),
            }
        }
