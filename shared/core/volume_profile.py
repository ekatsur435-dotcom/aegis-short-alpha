"""
Volume Profile v1.0 — POC-based SL (#31)

Рассчитывает Volume Profile из OHLCV свечей и находит Point of Control (POC) —
ценовой уровень с наибольшим торговым объёмом.

POC = самый ликвидный уровень → сильное Support/Resistance → идеальный SL.

SHORT: если POC выше текущей цены → SL чуть выше POC (зона сопротивления)
LONG:  если POC ниже текущей цены  → SL чуть ниже POC (зона поддержки)

ENV:
  USE_VP_SL          = true    включить Volume Profile SL
  VP_LOOKBACK        = 60      свечей для расчёта профиля
  VP_BINS            = 50      ценовых уровней в профиле
  VP_BUFFER_PCT      = 0.2     буфер за POC (%)
  VP_SL_MIN_PCT      = 1.0     мин SL% от цены
  VP_SL_MAX_PCT      = 8.0     макс SL% от цены
"""
import os
import logging
from typing import Optional, Tuple, List

logger = logging.getLogger(__name__)

_ENABLE      = os.getenv("USE_VP_SL",    "true").lower() == "true"
_LOOKBACK    = int(float(os.getenv("VP_LOOKBACK",  "60")))
_BINS        = int(float(os.getenv("VP_BINS",      "50")))
_BUFFER_PCT  = float(os.getenv("VP_BUFFER_PCT",    "0.2"))
_SL_MIN_PCT  = float(os.getenv("VP_SL_MIN_PCT",    "1.0"))
_SL_MAX_PCT  = float(os.getenv("VP_SL_MAX_PCT",    "8.0"))


def _build_profile(candles, num_bins: int) -> List[Tuple[float, float]]:
    """
    Строит Volume Profile: распределяет объём каждой свечи по ценовым уровням.

    Алгоритм:
      1. Разбиваем диапазон цен [min_low, max_high] на num_bins бинов
      2. Для каждой свечи распределяем quote_volume пропорционально перекрытию
         между диапазоном свечи [low, high] и каждым бином

    Returns: [(price_level, volume)] — середина бина и суммарный объём, old→new
    """
    if not candles or len(candles) < 3:
        return []

    price_min = min(c.low  for c in candles)
    price_max = max(c.high for c in candles)
    if price_max <= price_min:
        return []

    bin_size = (price_max - price_min) / num_bins
    bins     = [0.0] * num_bins

    for c in candles:
        c_range = c.high - c.low
        vol     = getattr(c, "quote_volume", 0) or 0
        if vol <= 0:
            continue

        if c_range <= 0:
            # Дожи — весь объём на close
            idx = min(int((c.close - price_min) / bin_size), num_bins - 1)
            bins[idx] += vol
            continue

        for i in range(num_bins):
            bin_low  = price_min + i * bin_size
            bin_high = bin_low   + bin_size
            overlap  = min(c.high, bin_high) - max(c.low, bin_low)
            if overlap > 0:
                bins[i] += vol * (overlap / c_range)

    result = []
    for i, v in enumerate(bins):
        mid = price_min + (i + 0.5) * bin_size
        result.append((mid, v))
    return result


def find_poc(candles, num_bins: int = 50) -> Optional[float]:
    """
    Возвращает Point of Control — ценовой уровень с наибольшим объёмом.
    """
    profile = _build_profile(candles, num_bins)
    if not profile:
        return None
    poc_price, _ = max(profile, key=lambda x: x[1])
    return poc_price


def find_value_area(candles, num_bins: int = 50,
                    va_pct: float = 70.0) -> Optional[Tuple[float, float, float]]:
    """
    Возвращает Value Area — диапазон цен содержащий va_pct% объёма.
    Returns: (va_low, poc, va_high) или None.
    """
    profile = _build_profile(candles, num_bins)
    if not profile:
        return None

    total = sum(v for _, v in profile)
    if total <= 0:
        return None

    target  = total * va_pct / 100
    poc_idx = max(range(len(profile)), key=lambda i: profile[i][1])
    poc_p   = profile[poc_idx][0]

    lo_idx = hi_idx = poc_idx
    included = profile[poc_idx][1]

    while included < target:
        can_lo = lo_idx > 0
        can_hi = hi_idx < len(profile) - 1
        if not can_lo and not can_hi:
            break
        vol_lo = profile[lo_idx - 1][1] if can_lo else -1
        vol_hi = profile[hi_idx + 1][1] if can_hi else -1
        if vol_hi >= vol_lo:
            hi_idx   += 1
            included += profile[hi_idx][1]
        else:
            lo_idx   -= 1
            included += profile[lo_idx][1]

    return profile[lo_idx][0], poc_p, profile[hi_idx][0]


def calculate_poc_sl(candles, price: float, direction: str) -> Tuple[Optional[float], str]:
    """
    #31 Volume Profile POC Stop Loss.

    Использует Point of Control как уровень SL — самый ликвидный уровень
    рынка является мощной зоной S/R.

    SHORT: POC выше цены → SL за POC (зона сопротивления с максимальным объёмом)
    LONG:  POC ниже цены  → SL за POC (зона поддержки с максимальным объёмом)

    Args:
        candles:   список CandleData (old→new)
        price:     текущая цена
        direction: "short" или "long"

    Returns:
        (sl_price, description) или (None, причина)
    """
    if not _ENABLE:
        return None, "[VP SL] disabled"
    if not candles or len(candles) < 20:
        return None, "[VP SL] недостаточно свечей"

    try:
        lookback = min(_LOOKBACK, len(candles))
        poc = find_poc(candles[-lookback:], _BINS)

        if poc is None:
            return None, "[VP SL] Volume Profile пуст"

        if direction == "short":
            if poc <= price:
                return None, f"[VP SL] POC={poc:.6f} ниже цены — не для SHORT SL"
            sl_price = round(poc * (1 + _BUFFER_PCT / 100), 8)
            sl_pct   = (sl_price - price) / price * 100
            if not (_SL_MIN_PCT <= sl_pct <= _SL_MAX_PCT):
                return None, f"[VP SL] SL {sl_pct:.1f}% вне [{_SL_MIN_PCT}%–{_SL_MAX_PCT}%]"
            return sl_price, f"📊 [VP SL] POC={poc:.6f} → SL={sl_price:.6f} ({sl_pct:.2f}%)"

        else:  # long
            if poc >= price:
                return None, f"[VP SL] POC={poc:.6f} выше цены — не для LONG SL"
            sl_price = round(poc * (1 - _BUFFER_PCT / 100), 8)
            sl_pct   = (price - sl_price) / price * 100
            if not (_SL_MIN_PCT <= sl_pct <= _SL_MAX_PCT):
                return None, f"[VP SL] SL {sl_pct:.1f}% вне диапазона"
            return sl_price, f"📊 [VP SL] POC={poc:.6f} → SL={sl_price:.6f} ({sl_pct:.2f}%)"

    except Exception as e:
        logger.debug(f"[VP SL] error: {e}")
        return None, f"[VP SL] error: {e}"
