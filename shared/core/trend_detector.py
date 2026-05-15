"""
Trend Following Detector v1.0
Детектирует трендовые движения БЕЗ лагающих индикаторов (no EMA, no SMA, no ADX).

Метрики (leading/coincident only):
  1. 4H momentum : |price_change_4h| ≥ TREND_MOMENTUM_4H %
  2. Volume surge : текущий объём > avg(20 баров) × TREND_VOLUME_MULT
  3. 1H alignment : |price_change_1h| ≥ TREND_MOMENTUM_1H % (тот же знак)

2/3 условий → тренд подтверждён → score_bonus + extend_tp
3/3 условий → сильный тренд   → score_bonus × 1.3

Интеграция:
  → score_bonus добавляется к base_score ДО финального фильтра
  → extend_tp=True разрешает 6 TP уровней (вместо 4) для этой сделки

ENV:
  ENABLE_TREND_DETECTOR = true    включить модуль (дефолт true)
  TREND_MOMENTUM_4H     = 1.5     мин изменение за 4H (%)
  TREND_MOMENTUM_1H     = 0.3     мин изменение за 1H (%)
  TREND_VOLUME_MULT     = 1.3     порог объёма (× avg20)
  TREND_SCORE_BONUS     = 8       бонус очков при 2/3 условиях
  TREND_EXTEND_TP       = true    разрешить 6 TP при подтверждённом тренде
"""
import os
import logging
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger(__name__)

_ENABLE       = os.getenv("ENABLE_TREND_DETECTOR", "true").lower() == "true"
_MOMENTUM_4H  = float(os.getenv("TREND_MOMENTUM_4H", "1.5"))
_MOMENTUM_1H  = float(os.getenv("TREND_MOMENTUM_1H", "0.3"))
_VOLUME_MULT  = float(os.getenv("TREND_VOLUME_MULT", "1.3"))
_SCORE_BONUS  = int(os.getenv("TREND_SCORE_BONUS", "8"))
_EXTEND_TP    = os.getenv("TREND_EXTEND_TP", "true").lower() == "true"


@dataclass
class TrendResult:
    has_trend:   bool  = False
    score_bonus: int   = 0
    extend_tp:   bool  = False
    conditions:  int   = 0     # 0-3 сколько из 3 условий выполнено
    description: str   = ""


_NO_TREND = TrendResult()


def detect_trend(
    candles_4h,
    price_change_1h:    float = 0.0,
    price_change_4h:    float = 0.0,
    volume_spike_ratio: float = 1.0,
    direction:          str   = "long",
) -> TrendResult:
    """
    Детектирует трендовое движение по price momentum + volume (no EMA).

    Args:
        candles_4h:         список CandleData 4H (old→new), мин 10 свечей
        price_change_1h:    % изменение цены за 1H (из MarketData)
        price_change_4h:    % изменение цены за 4H (из MarketData)
        volume_spike_ratio: текущий объём / avg объём (из MarketData)
        direction:          "long" или "short"

    Returns:
        TrendResult
    """
    if not _ENABLE:
        return _NO_TREND

    if not candles_4h or len(candles_4h) < 5:
        return _NO_TREND

    try:
        sign = 1 if direction == "long" else -1
        met: List[str] = []

        # ── 1. 4H momentum ──────────────────────────────────────────────
        p4h_signed = (price_change_4h or 0.0) * sign
        if p4h_signed >= _MOMENTUM_4H:
            met.append(f"4H={price_change_4h:+.1f}%")

        # ── 2. Volume surge ─────────────────────────────────────────────
        # Предпочитаем готовый volume_spike_ratio из MarketData.
        # Если не передан (≤0) — считаем из свечей напрямую.
        vol_ratio = volume_spike_ratio or 0.0
        if vol_ratio <= 0 and len(candles_4h) >= 21:
            current_vol = candles_4h[-1].volume
            avg_vol     = sum(c.volume for c in candles_4h[-21:-1]) / 20
            vol_ratio   = current_vol / avg_vol if avg_vol > 0 else 1.0

        if vol_ratio >= _VOLUME_MULT:
            met.append(f"Vol×{vol_ratio:.1f}")

        # ── 3. 1H alignment ─────────────────────────────────────────────
        p1h_signed = (price_change_1h or 0.0) * sign
        if p1h_signed >= _MOMENTUM_1H:
            met.append(f"1H={price_change_1h:+.1f}%")

        # ── Итог ────────────────────────────────────────────────────────
        n = len(met)
        if n < 2:
            return _NO_TREND

        bonus  = _SCORE_BONUS if n == 2 else min(int(_SCORE_BONUS * 1.3), 15)
        extend = _EXTEND_TP
        emoji  = "📈" if direction == "long" else "📉"
        desc   = f"{emoji} [TREND {n}/3] {' | '.join(met)} → +{bonus}"

        return TrendResult(
            has_trend=True,
            score_bonus=bonus,
            extend_tp=extend,
            conditions=n,
            description=desc,
        )

    except Exception as e:
        logger.debug(f"[TrendDetector] {e}")
        return _NO_TREND
