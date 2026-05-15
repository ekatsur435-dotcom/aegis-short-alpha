"""
Pattern Detector v3.0 — ЕДИНЫЙ ФАЙЛ (Long + Short)

ЗАМЕНЯЕТ оба файла:
  - pattern_detector.py (старый)
  - pattern_detector_v2.py (удалить!)

ИСПРАВЛЕНИЕ v3.0:
  ✅ PatternResult.strength → alias для score_bonus
     Исправляет: 'PatternResult' object has no attribute 'strength'
     Scorer.py использует p.strength — теперь работает с обоими файлами

ПАТТЕРНЫ:
  LONG:  MEGA_LONG, TRAP_SHORT, REJECTION_LONG,
         BREAKOUT_LONG, MOMENTUM_LONG, LIQUIDITY_SWEEP_LONG,
         CONSOLIDATION_BREAK_LONG, WYCKOFF_SPRING
  SHORT: MEGA_SHORT, TRAP_LONG, REJECTION_SHORT,
         BREAKOUT_SHORT, MOMENTUM_SHORT, LIQUIDITY_SWEEP_SHORT,
         DISTRIBUTION_BREAK, WYCKOFF_UPTHRUST
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ============================================================================
# PatternResult — ЕДИНЫЙ датакласс для всех паттернов
# ============================================================================

@dataclass
class PatternResult:
    name: str
    score_bonus: int       # основная метрика силы (0-30)
    confidence: float      # 0.0-1.0
    direction: str         # "long" | "short"
    suggested_sl_pct: float = 0.0
    suggested_tp1_pct: float = 0.0
    reasons: List[str] = field(default_factory=list)

    # ── ОБРАТНАЯ СОВМЕСТИМОСТЬ: scorer.py использует p.strength ─────────────
    # Исправляет: 'PatternResult' object has no attribute 'strength'
    @property
    def strength(self) -> int:
        """Alias для score_bonus (обратная совместимость со старым scorer.py)."""
        return self.score_bonus

    # Поля из старого Pattern датакласса (для scorer.py)
    @property
    def candles_ago(self) -> int:
        return 0

    @property
    def freshness(self) -> int:
        return 2


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

def _closes(candles) -> List[float]:
    return [c.close for c in candles]

def _highs(candles) -> List[float]:
    return [c.high for c in candles]

def _lows(candles) -> List[float]:
    return [c.low for c in candles]

def _avg_vol(candles, lookback: int = 20) -> float:
    vols = [c.quote_volume for c in candles]
    if len(vols) < lookback:
        return sum(vols) / len(vols) if vols else 1.0
    return sum(vols[-lookback-1:-1]) / lookback

def _vol_spike(candles, lookback: int = 20) -> float:
    avg = _avg_vol(candles, lookback)
    if avg <= 0:
        return 1.0
    return candles[-1].quote_volume / avg

def _atr(candles, period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        pc  = candles[i-1].close
        tr  = max(candles[i].high - candles[i].low,
                  abs(candles[i].high - pc), abs(candles[i].low - pc))
        trs.append(tr)
    return sum(trs[-period:]) / period

def _body(c) -> float:
    return abs(c.close - c.open)

def _ema(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return values
    k   = 2 / (period + 1)
    ema = [sum(values[:period]) / period]
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema

def _swing_highs(candles, lookback: int = 3) -> List[float]:
    highs = []
    for i in range(lookback, len(candles) - lookback):
        h = candles[i].high
        if (all(candles[j].high <= h for j in range(i-lookback, i)) and
                all(candles[j].high <= h for j in range(i+1, i+lookback+1))):
            highs.append(h)
    return highs

def _swing_lows(candles, lookback: int = 3) -> List[float]:
    lows = []
    for i in range(lookback, len(candles) - lookback):
        l = candles[i].low
        if (all(candles[j].low >= l for j in range(i-lookback, i)) and
                all(candles[j].low >= l for j in range(i+1, i+lookback+1))):
            lows.append(l)
    return lows

def _swing_highs_idx(candles, lookback: int = 3) -> List[Tuple[int, float]]:
    """Swing highs с индексами: [(idx, price), ...]"""
    result = []
    for i in range(lookback, len(candles) - lookback):
        h = candles[i].high
        if (all(candles[j].high <= h for j in range(i - lookback, i)) and
                all(candles[j].high <= h for j in range(i + 1, i + lookback + 1))):
            result.append((i, h))
    return result

def _swing_lows_idx(candles, lookback: int = 3) -> List[Tuple[int, float]]:
    """Swing lows с индексами: [(idx, price), ...]"""
    result = []
    for i in range(lookback, len(candles) - lookback):
        l = candles[i].low
        if (all(candles[j].low >= l for j in range(i - lookback, i)) and
                all(candles[j].low >= l for j in range(i + 1, i + lookback + 1))):
            result.append((i, l))
    return result


# ============================================================================
# LONG PATTERN DETECTOR
# ============================================================================

class LongPatternDetector:
    """Детектор паттернов для LONG входов."""

    def detect_all(self, candles, hourly_deltas=None, market_data=None) -> List[PatternResult]:
        results = []
        for fn in [
            self.detect_ict_unicorn_long,   # highest priority — confluence signal
            self.detect_ote_long,
            self.detect_breaker_long,
            self.detect_fvg_long,
            self.detect_breakout_long,
            self.detect_momentum_long,
            self.detect_liquidity_sweep_long,
            self.detect_consolidation_break_long,
            self.detect_wyckoff_spring,
            self.detect_mega_long,
            self.detect_trap_short,
            self.detect_rejection_long,
        ]:
            try:
                r = fn(candles, hourly_deltas, market_data)
                if r:
                    results.append(r)
            except Exception:
                pass
        results.sort(key=lambda x: x.score_bonus, reverse=True)
        return results

    def _get_price_trend(self, candles) -> str:
        if len(candles) < 20:
            return "flat"
        closes = _closes(candles)
        ema20  = _ema(closes, 20)
        if not ema20:
            return "flat"
        slope = (ema20[-1] - ema20[-5]) / ema20[-5] * 100 if len(ema20) >= 5 else 0
        if closes[-1] > ema20[-1] and slope > 0.1:
            return "up"
        elif closes[-1] < ema20[-1] and slope < -0.1:
            return "down"
        return "flat"

    # ── ICT / SMC ПАТТЕРНЫ ───────────────────────────────────────────────────

    def detect_ote_long(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """OTE_LONG: Fibonacci Optimal Trade Entry — pullback 61.8%-79% после медвежьего импульса.

        ICT OTE зона: после сильного падения (swing_high→swing_low) цена
        восстанавливается в зону 61.8%-79% от хода → LONG вход.
        Self-fulfilling: тысячи трейдеров смотрят 61.8% → уровень работает.
        score_bonus ≈ 18-22
        """
        if len(candles) < 20:
            return None
        atr_v = _atr(candles, 14)
        if atr_v <= 0:
            return None

        current_price = candles[-1].close
        lookback = min(60, len(candles) - 3)
        window = candles[-lookback:]
        best = None

        sh_list = _swing_highs_idx(window, lookback=3)
        sl_list = _swing_lows_idx(window,  lookback=3)
        if not sh_list or not sl_list:
            return None

        # Ищем пары (swing_high → swing_low) — медвежий импульс
        for sh_idx, sh_price in sh_list:
            # Swing low должен быть ПОСЛЕ swing high
            candidates = [(si, sl) for si, sl in sl_list if si > sh_idx and sl < sh_price]
            if not candidates:
                continue
            # Берём ближайший swing low после swing high
            sl_idx, sl_price = min(candidates, key=lambda x: x[0])

            impulse = sh_price - sl_price
            if impulse < atr_v * 1.5:  # Импульс слишком мал
                continue

            # OTE зона: 61.8% – 79% восстановления от sl_price вверх
            ote_lo = sl_price + 0.618 * impulse
            ote_hi = sl_price + 0.79  * impulse
            if ote_hi <= ote_lo:
                continue

            # Цена должна быть в OTE зоне (или в пределах 0.5% у границ)
            in_zone = (ote_lo * 0.995 <= current_price <= ote_hi * 1.005)
            if not in_zone:
                continue

            impulse_pct = impulse / sl_price * 100
            if impulse_pct < 2.0:  # Минимальный значимый ход
                continue

            # Предпочитаем более свежие и крупные импульсы
            dist = abs((ote_lo + ote_hi) / 2 - current_price)
            if best is None or (impulse_pct > best[0] and dist < best[1] * 1.5):
                best = (impulse_pct, dist, ote_lo, ote_hi, sh_price, sl_price, impulse)

        if best is None:
            return None

        imp_pct, _, ote_lo, ote_hi, sh_price, sl_price, impulse = best
        fib_lvl = (current_price - sl_price) / impulse * 100
        bonus = min(22, int(14 + imp_pct * 0.5))
        return PatternResult(
            name="OTE_LONG", score_bonus=bonus,
            confidence=min(0.78, 0.56 + imp_pct * 0.01),
            direction="long",
            suggested_sl_pct=round((current_price - sl_price * 0.995) / current_price * 100 + 0.2, 2),
            reasons=[
                f"📐 OTE LONG [{ote_lo:.6f} – {ote_hi:.6f}] Fib {fib_lvl:.1f}%",
                f"Импульс {sh_price:.6f}→{sl_price:.6f} (-{imp_pct:.1f}%) | OTE pullback 61.8-79%",
            ],
        )

    def detect_ict_unicorn_long(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """ICT_UNICORN_LONG: Bullish FVG ∩ Bullish Breaker Block = сильнейший ICT сигнал 2025.

        Когда незаполненный FVG совпадает с зоной Breaker Block —
        два независимых магнита institutional money указывают на одну зону.
        Win rate 75%+, score_bonus ≈ 28-30.
        """
        if len(candles) < 20:
            return None
        atr_v = _atr(candles, 14)
        if atr_v <= 0:
            return None

        current_price = candles[-1].close
        lookback = min(50, len(candles) - 3)
        fvg_zones   = []
        break_zones = []

        # ── Собираем все незаполненные Bullish FVG ────────────────────────
        for i in range(len(candles) - lookback, len(candles) - 2):
            c1, c2, c3 = candles[i], candles[i + 1], candles[i + 2]
            if c2.close <= c2.open:
                continue
            body2 = c2.close - c2.open
            if body2 < atr_v * 1.0:  # Чуть мягче чем standalone FVG
                continue
            fvg_lo, fvg_hi = c1.high, c3.low
            if fvg_hi <= fvg_lo:
                continue
            if (fvg_hi - fvg_lo) / c2.close * 100 < 0.08:
                continue
            filled = any(candles[j].low <= fvg_lo for j in range(i + 3, len(candles)))
            if not filled:
                fvg_zones.append((fvg_lo, fvg_hi))

        if not fvg_zones:
            return None

        # ── Собираем все Bullish Breaker Block зоны ───────────────────────
        lb2 = min(50, len(candles) - 5)
        for i in range(len(candles) - lb2, len(candles) - 6):
            c_ob = candles[i]
            if c_ob.close <= c_ob.open:  # Должна быть бычья (перед медвежьим импульсом)
                continue
            ob_lo, ob_hi = c_ob.low, c_ob.high
            end = min(i + 5, len(candles) - 1)
            lo_after = min(candles[j].low for j in range(i + 1, end + 1))
            if (ob_lo - lo_after) / ob_lo * 100 < 1.5:
                continue
            broken = any(candles[j].high > ob_hi for j in range(i + 2, len(candles) - 1))
            if not broken:
                continue
            break_zones.append((ob_lo, ob_hi))

        if not break_zones:
            return None

        # ── Ищем пересечение FVG и Breaker Block ──────────────────────────
        best = None
        for flo, fhi in fvg_zones:
            for blo, bhi in break_zones:
                overlap_lo = max(flo, blo)
                overlap_hi = min(fhi, bhi)
                if overlap_hi <= overlap_lo:
                    continue  # Нет пересечения
                # Цена в зоне пересечения
                if current_price < overlap_lo * 0.99 or current_price > overlap_hi * 1.01:
                    continue
                overlap_pct = (overlap_hi - overlap_lo) / current_price * 100
                if overlap_pct < 0.05:
                    continue
                dist = abs((overlap_lo + overlap_hi) / 2 - current_price)
                if best is None or dist < best[0]:
                    best = (dist, overlap_lo, overlap_hi, flo, fhi, blo, bhi, overlap_pct)

        if best is None:
            return None

        _, ov_lo, ov_hi, flo, fhi, blo, bhi, ov_pct = best
        bonus = min(30, int(24 + ov_pct * 3))
        return PatternResult(
            name="ICT_UNICORN_LONG", score_bonus=bonus,
            confidence=min(0.88, 0.72 + ov_pct * 0.05),
            direction="long",
            suggested_sl_pct=round((current_price - ov_lo * 0.993) / current_price * 100 + 0.3, 2),
            reasons=[
                f"🦄 ICT UNICORN LONG | Overlap [{ov_lo:.6f}–{ov_hi:.6f}]",
                f"Bullish FVG [{flo:.6f}–{fhi:.6f}] ∩ Breaker [{blo:.6f}–{bhi:.6f}]",
                "FVG + Breaker Block в одной зоне = institutional confluence 75%+",
            ],
        )

    def detect_breaker_long(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """BREAKER_LONG: Пробитый медвежий OB → зона поддержки (ICT Bullish Breaker Block).
        1. Bearish OB = бычья свеча перед медвежьим импульсом
        2. Цена пробивает OB вверх → OB флипает в поддержку
        3. Цена возвращается в бывшую OB зону → LONG вход
        """
        if len(candles) < 15:
            return None
        atr_v = _atr(candles, 14)
        if atr_v <= 0:
            return None

        current_price = candles[-1].close
        lookback = min(50, len(candles) - 5)
        best = None

        for i in range(len(candles) - lookback, len(candles) - 6):
            c_ob = candles[i]

            # OB candle: последняя бычья свеча перед медвежьим импульсом
            if c_ob.close <= c_ob.open:
                continue
            ob_low  = c_ob.low
            ob_high = c_ob.high

            # Медвежий импульс после OB (≥1.5% падения)
            end = min(i + 5, len(candles) - 1)
            lo_after = min(candles[j].low for j in range(i + 1, end + 1))
            drop_pct = (ob_low - lo_after) / ob_low * 100
            if drop_pct < 1.5:
                continue

            # Цена пробивает OB вверх (проходит выше ob_high)
            broken_idx = -1
            for j in range(i + 2, len(candles) - 1):
                if candles[j].high > ob_high:
                    broken_idx = j
                    break
            if broken_idx < 0:
                continue

            # Цена возвращается В зону OB (pullback к бывшему сопротивлению = поддержка)
            if current_price > ob_high * 1.01:
                continue
            if current_price < ob_low * 0.985:
                continue

            # Хотя бы одна свеча после пробоя вернулась в зону
            retest = any(candles[j].low <= ob_high for j in range(broken_idx + 1, len(candles)))
            if not retest:
                continue

            gap_pct = (ob_high - ob_low) / current_price * 100
            dist = abs((ob_low + ob_high) / 2 - current_price)
            if best is None or dist < best[0]:
                best = (dist, ob_low, ob_high, drop_pct, gap_pct)

        if best is None:
            return None

        _, ob_low, ob_high, drop_pct, gap_pct = best
        bonus = min(26, int(18 + min(drop_pct, 10) * 0.5 + gap_pct * 2))
        return PatternResult(
            name="BREAKER_LONG", score_bonus=bonus,
            confidence=min(0.82, 0.62 + min(drop_pct, 15) * 0.01),
            direction="long",
            suggested_sl_pct=round((current_price - ob_low * 0.995) / current_price * 100 + 0.2, 2),
            reasons=[
                f"🟢 Bullish Breaker [{ob_low:.6f} – {ob_high:.6f}]",
                f"Медвежий OB -{drop_pct:.1f}% пробит вверх → pullback = поддержка",
            ],
        )

    def detect_fvg_long(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """FVG_LONG: Bullish Fair Value Gap — цена в зоне незаполненного гэпа (ICT поддержка).
        3 свечи: c1 | c2(сильный бычий импульс) | c3. Гэп = c1.high < c3.low.
        Вход когда цена возвращается в гэп (retest зоны поддержки).
        """
        if len(candles) < 10:
            return None
        atr_v = _atr(candles, 14)
        if atr_v <= 0:
            return None

        current_price = candles[-1].close
        lookback = min(20, len(candles) - 2)
        best = None

        for i in range(len(candles) - lookback, len(candles) - 2):
            c1 = candles[i]
            c2 = candles[i + 1]
            c3 = candles[i + 2]

            # Средняя свеча — сильный бычий импульс
            if c2.close <= c2.open:
                continue
            body2 = c2.close - c2.open
            if body2 < atr_v * 1.2:
                continue

            # FVG: гэп между c1.high и c3.low
            fvg_lo = c1.high
            fvg_hi = c3.low
            if fvg_hi <= fvg_lo:
                continue

            gap_pct = (fvg_hi - fvg_lo) / c2.close * 100
            if gap_pct < 0.1:
                continue

            # Гэп ещё не заполнен (цена не возвращалась ниже c1.high)
            filled = any(candles[j].low <= fvg_lo for j in range(i + 3, len(candles)))
            if filled:
                continue

            # Цена в зоне гэпа или подходит снизу (буфер 2%)
            if current_price < fvg_lo * 0.98:
                continue
            if current_price > fvg_hi * 1.005:
                continue

            dist = abs((fvg_lo + fvg_hi) / 2 - current_price)
            if best is None or dist < best[0]:
                best = (dist, fvg_lo, fvg_hi, gap_pct, body2)

        if best is None:
            return None

        _, fvg_lo, fvg_hi, gap_pct, body2 = best
        bonus = min(22, int(14 + gap_pct * 4))
        return PatternResult(
            name="FVG_LONG", score_bonus=bonus,
            confidence=min(0.80, 0.58 + gap_pct * 0.08),
            direction="long",
            suggested_sl_pct=round((current_price - fvg_lo * 0.995) / current_price * 100 + 0.2, 2),
            reasons=[
                f"📊 Bullish FVG [{fvg_lo:.6f} – {fvg_hi:.6f}] ({gap_pct:.2f}%)",
                f"Импульс {body2 / atr_v:.1f}×ATR | Незаполненный гэп = поддержка",
            ],
        )

    # ── НОВЫЕ ПАТТЕРНЫ ────────────────────────────────────────────────────────

    def detect_breakout_long(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """BREAKOUT_LONG: Пробой флэта вверх с объёмом."""
        if len(candles) < 25:
            return None
        consolidation = candles[-20:-2]
        last          = candles[-1]
        high_cons     = max(c.high for c in consolidation)
        low_cons      = min(c.low  for c in consolidation)
        cons_range    = (high_cons - low_cons) / low_cons * 100 if low_cons else 999
        if cons_range > 3.0 or last.close <= high_cons:
            return None
        vol_spike = _vol_spike(candles, 20)
        if vol_spike < 1.5:
            return None
        rng = last.high - last.low
        if rng > 0 and (last.close - last.open) / rng < 0.5:
            return None
        breakout_pct = (last.close - high_cons) / high_cons * 100
        bonus = min(25, int(10 + vol_spike * 3 + breakout_pct * 2))
        return PatternResult(
            name="BREAKOUT_LONG", score_bonus=bonus,
            confidence=min(0.9, 0.5 + vol_spike * 0.1), direction="long",
            suggested_sl_pct=round((last.close - low_cons) / last.close * 100, 2),
            reasons=[f"Breakout выше {high_cons:.4f} (флэт {cons_range:.1f}%)",
                     f"Volume spike {vol_spike:.1f}x | +{breakout_pct:.2f}%"],
        )

    def detect_momentum_long(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """MOMENTUM_LONG: Сильная бычья свеча + volume spike ≥2x."""
        if len(candles) < 25:
            return None
        last      = candles[-1]
        atr_v     = _atr(candles, 14)
        vol_spike = _vol_spike(candles, 20)
        if vol_spike < 2.0:
            return None
        rng = last.high - last.low
        if rng <= 0 or (last.close - last.low) / rng < 0.65:
            return None
        body = last.close - last.open
        if body <= 0 or (atr_v > 0 and body < atr_v * 0.5):
            return None
        closes = _closes(candles)
        ema20  = _ema(closes, 20)
        if ema20 and last.close < ema20[-1]:
            return None
        rsi = getattr(md, "rsi_1h", None) if md else None
        if rsi and (rsi < 40 or rsi > 78):
            return None
        pct_move = (last.close - last.open) / last.open * 100 if last.open else 0
        bonus    = min(20, int(12 + vol_spike * 1.5))
        return PatternResult(
            name="MOMENTUM_LONG", score_bonus=bonus,
            confidence=min(0.85, 0.55 + vol_spike * 0.08), direction="long",
            suggested_sl_pct=round((last.close - last.open) / last.close * 100, 2),
            reasons=[f"Momentum свеча +{pct_move:.2f}% | Volume {vol_spike:.1f}x avg"],
        )

    def detect_liquidity_sweep_long(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """LIQUIDITY_SWEEP_LONG: Stop hunt вниз → разворот (ICT/SMC).
        
        v2: sweep.close может быть чуть ниже recent_sl (буфер 0.5%) —
        на криптоалтах свеча часто закрывается в зоне swept lows, а не выше.
        """
        if len(candles) < 20:
            return None
        last  = candles[-1]
        sweep = candles[-2]
        atr_v = _atr(candles[:-2], 14)
        if atr_v <= 0:
            return None
        lower_wick = min(sweep.open, sweep.close) - sweep.low
        if lower_wick < atr_v * 1.0:
            return None
        swing_lows_list = _swing_lows(candles[:-3], lookback=3)
        if not swing_lows_list:
            return None
        recent_sl = (min(swing_lows_list[-3:]) if len(swing_lows_list) >= 3
                     else swing_lows_list[-1])
        if sweep.low > recent_sl:
            return None
        # ✅ FIX v2: буфер 0.5% — sweep.close может чуть не дойти выше recent_sl
        if sweep.close < recent_sl * 0.995:
            return None
        if last.close <= last.open:
            return None
        vol_spike    = _vol_spike(candles[:-1], 20)
        sweep_depth  = (recent_sl - sweep.low) / atr_v
        bonus        = min(25, int(18 + sweep_depth * 2 + vol_spike))
        return PatternResult(
            name="LIQUIDITY_SWEEP_LONG", score_bonus=bonus,
            confidence=0.75, direction="long",
            suggested_sl_pct=round((last.close - sweep.low) / last.close * 100 + 0.2, 2),
            reasons=[f"Ликвидность снята под {recent_sl:.4f}",
                     f"Sweep {sweep_depth:.1f}x ATR | Volume {vol_spike:.1f}x",
                     "Цена вернулась выше уровня — разворот"],
        )

    def detect_consolidation_break_long(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """CONSOLIDATION_BREAK_LONG: Выход из боковика вверх."""
        if len(candles) < 25:
            return None
        cons       = candles[-22:-2]
        last       = candles[-1]
        high_cons  = max(c.high for c in cons)
        low_cons   = min(c.low  for c in cons)
        range_pct  = (high_cons - low_cons) / low_cons * 100 if low_cons else 999
        if range_pct > 2.5 or last.close <= high_cons:
            return None
        vol_spike    = _vol_spike(candles, 20)
        if vol_spike < 1.3:
            return None
        breakout_pct = (last.close - high_cons) / high_cons * 100
        bonus        = min(18, int(10 + vol_spike * 2 + breakout_pct))
        return PatternResult(
            name="CONSOLIDATION_BREAK_LONG", score_bonus=bonus,
            confidence=0.65, direction="long",
            suggested_sl_pct=round((last.close - low_cons) / last.close * 100, 2),
            reasons=[f"Флэт {range_pct:.1f}% ({len(cons)} свечей) → пробой +{breakout_pct:.2f}%",
                     f"Volume {vol_spike:.1f}x"],
        )

    def detect_wyckoff_spring(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """WYCKOFF_SPRING: Ложный пробой лоу диапазона накопления.
        
        v2: расширен диапазон 1.5-25% (крипто-алты имеют широкие диапазоны),
        ослаблен volume фильтр (2.5x вместо 1.5x),
        допускается слабо-медвежья последняя свеча при wick-восстановлении.
        """
        if len(candles) < 30:
            return None
        acc    = candles[-30:-5]
        last   = candles[-1]
        prev   = candles[-2]
        r_high = max(c.high for c in acc)
        r_low  = min(c.low  for c in acc)
        r_pct  = (r_high - r_low) / r_low * 100 if r_low else 999
        # ✅ FIX v2: крипто-алты имеют диапазоны 10-25% — расширяем порог
        if not (1.5 < r_pct < 25.0):
            return None
        if prev.low > r_low or prev.close < r_low:
            return None
        avg_vol = _avg_vol(acc, min(len(acc), 15))
        # ✅ FIX v2: расслабляем volume фильтр — памп на спринге допустим
        if prev.quote_volume > avg_vol * 2.5:
            return None
        if last.close < r_low:
            return None
        # ✅ FIX v2: допускаем слабо-медвежью свечу если lower wick показывает отскок
        lower_wick_last = min(last.open, last.close) - last.low
        atr_v = _atr(candles[-15:], 14)
        is_recovering = last.close > last.open or lower_wick_last > atr_v * 0.3
        if not is_recovering:
            return None
        spring_depth = (r_low - prev.low) / r_low * 100
        # Бонус выше для широких диапазонов (более значимые паттерны)
        bonus = 12 if r_pct < 8.0 else 10
        return PatternResult(
            name="WYCKOFF_SPRING", score_bonus=bonus,
            confidence=0.80, direction="long",
            suggested_sl_pct=round((last.close - prev.low) / last.close * 100 + 0.3, 2),
            reasons=[f"Wyckoff Spring: диапазон {r_pct:.1f}%",
                     f"Spring -{spring_depth:.2f}% ниже поддержки",
                     "Объём в норме — ложный пробой"],
        )

    # ── КЛАССИЧЕСКИЕ ПАТТЕРНЫ ─────────────────────────────────────────────────

    def detect_mega_long(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        if len(candles) < 21:
            return None
        rsi = getattr(md, "rsi_1h", None) if md else None
        if rsi and rsi > 45:
            return None
        last       = candles[-2]  # закрытая свеча — стабильный сигнал
        body       = _body(last)
        lower_wick = min(last.open, last.close) - last.low
        if lower_wick < body * 1.5:
            return None
        vol_spike = _vol_spike(candles, 20)
        if vol_spike < 1.2:
            return None
        return PatternResult(
            name="MEGA_LONG", score_bonus=20, confidence=0.6, direction="long",
            reasons=["RSI перепродан" if rsi else "Нижний wick",
                     f"Lower wick {lower_wick:.4f} | Volume {vol_spike:.1f}x"],
        )

    def detect_trap_short(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        if len(candles) < 20:
            return None
        last  = candles[-1]
        prev  = candles[-2]
        atr_v = _atr(candles[:-2], 14)
        prev_lower = min(prev.open, prev.close) - prev.low
        if prev_lower < atr_v * 0.8 or last.close <= last.open:
            return None
        return PatternResult(
            name="TRAP_SHORT", score_bonus=18, confidence=0.58, direction="long",
            reasons=["Шортисты пойманы в ловушку", "Разворот вверх подтверждён"],
        )

    def detect_rejection_long(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        if len(candles) < 11:
            return None
        last       = candles[-2]  # закрытая свеча — стабильный сигнал
        lower_wick = min(last.open, last.close) - last.low
        body       = _body(last)
        if lower_wick < body * 1.0 or last.close < last.open:
            return None
        return PatternResult(
            name="REJECTION_LONG", score_bonus=15, confidence=0.55, direction="long",
            reasons=["Отскок от поддержки", f"Lower wick {lower_wick:.4f}"],
        )


# ============================================================================
# SHORT PATTERN DETECTOR
# ============================================================================

class ShortPatternDetector:
    """Детектор паттернов для SHORT входов."""

    def detect_all(self, candles, hourly_deltas=None, market_data=None) -> List[PatternResult]:
        results = []
        for fn in [
            self.detect_ict_unicorn_short,  # highest priority — confluence signal
            self.detect_ote_short,
            self.detect_pump_dump_short,
            self.detect_breaker_short,
            self.detect_fvg_short,
            self.detect_breakout_short,
            self.detect_momentum_short,
            self.detect_liquidity_sweep_short,
            self.detect_distribution_break,
            self.detect_wyckoff_upthrust,
            self.detect_mega_short,
            self.detect_trap_long,
            self.detect_rejection_short,
        ]:
            try:
                r = fn(candles, hourly_deltas, market_data)
                if r:
                    results.append(r)
            except Exception:
                pass
        results.sort(key=lambda x: x.score_bonus, reverse=True)
        return results

    def _get_price_trend(self, candles) -> str:
        if len(candles) < 20:
            return "flat"
        closes = _closes(candles)
        ema20  = _ema(closes, 20)
        if not ema20:
            return "flat"
        slope = (ema20[-1] - ema20[-5]) / ema20[-5] * 100 if len(ema20) >= 5 else 0
        if closes[-1] < ema20[-1] and slope < -0.1:
            return "down"
        elif closes[-1] > ema20[-1] and slope > 0.1:
            return "up"
        return "flat"

    # ── ICT / SMC ПАТТЕРНЫ ───────────────────────────────────────────────────

    def detect_ote_short(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """OTE_SHORT: Fibonacci Optimal Trade Entry — pullback 61.8%-79% после бычьего импульса.

        После сильного роста (swing_low→swing_high) цена откатывает в зону
        61.8%-79% от хода → SHORT вход. Комбинируется с FVG = Unicorn.
        score_bonus ≈ 18-22
        """
        if len(candles) < 20:
            return None
        atr_v = _atr(candles, 14)
        if atr_v <= 0:
            return None

        current_price = candles[-1].close
        lookback = min(60, len(candles) - 3)
        window = candles[-lookback:]
        best = None

        sh_list = _swing_highs_idx(window, lookback=3)
        sl_list = _swing_lows_idx(window,  lookback=3)
        if not sh_list or not sl_list:
            return None

        # Ищем пары (swing_low → swing_high) — бычий импульс
        for sl_idx, sl_price in sl_list:
            candidates = [(si, sh) for si, sh in sh_list if si > sl_idx and sh > sl_price]
            if not candidates:
                continue
            sh_idx, sh_price = min(candidates, key=lambda x: x[0])

            impulse = sh_price - sl_price
            if impulse < atr_v * 1.5:
                continue

            # OTE зона: 61.8% – 79% отката от sh_price вниз
            ote_lo = sh_price - 0.79  * impulse
            ote_hi = sh_price - 0.618 * impulse
            if ote_hi <= ote_lo:
                continue

            in_zone = (ote_lo * 0.995 <= current_price <= ote_hi * 1.005)
            if not in_zone:
                continue

            impulse_pct = impulse / sl_price * 100
            if impulse_pct < 2.0:
                continue

            dist = abs((ote_lo + ote_hi) / 2 - current_price)
            if best is None or (impulse_pct > best[0] and dist < best[1] * 1.5):
                best = (impulse_pct, dist, ote_lo, ote_hi, sh_price, sl_price, impulse)

        if best is None:
            return None

        imp_pct, _, ote_lo, ote_hi, sh_price, sl_price, impulse = best
        fib_lvl = (sh_price - current_price) / impulse * 100
        bonus = min(22, int(14 + imp_pct * 0.5))
        return PatternResult(
            name="OTE_SHORT", score_bonus=bonus,
            confidence=min(0.78, 0.56 + imp_pct * 0.01),
            direction="short",
            suggested_sl_pct=round((sh_price * 1.005 - current_price) / current_price * 100 + 0.2, 2),
            reasons=[
                f"📐 OTE SHORT [{ote_lo:.6f} – {ote_hi:.6f}] Fib {fib_lvl:.1f}%",
                f"Импульс {sl_price:.6f}→{sh_price:.6f} (+{imp_pct:.1f}%) | OTE pullback 61.8-79%",
            ],
        )

    def detect_ict_unicorn_short(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """ICT_UNICORN_SHORT: Bearish FVG ∩ Bearish Breaker Block = сильнейший ICT сигнал 2025.

        Два независимых institutional магнита на одном уровне → высокая вероятность
        отбоя вниз. Win rate 75%+, score_bonus ≈ 28-30.
        """
        if len(candles) < 20:
            return None
        atr_v = _atr(candles, 14)
        if atr_v <= 0:
            return None

        current_price = candles[-1].close
        lookback = min(50, len(candles) - 3)
        fvg_zones   = []
        break_zones = []

        # ── Bearish FVG: c3.high < c1.low ─────────────────────────────────
        for i in range(len(candles) - lookback, len(candles) - 2):
            c1, c2, c3 = candles[i], candles[i + 1], candles[i + 2]
            if c2.close >= c2.open:
                continue
            body2 = c2.open - c2.close
            if body2 < atr_v * 1.0:
                continue
            fvg_lo, fvg_hi = c3.high, c1.low
            if fvg_hi <= fvg_lo:
                continue
            if (fvg_hi - fvg_lo) / c2.close * 100 < 0.08:
                continue
            filled = any(candles[j].high >= fvg_hi for j in range(i + 3, len(candles)))
            if not filled:
                fvg_zones.append((fvg_lo, fvg_hi))

        if not fvg_zones:
            return None

        # ── Bearish Breaker Block: бычий OB пробит вниз ───────────────────
        lb2 = min(50, len(candles) - 5)
        for i in range(len(candles) - lb2, len(candles) - 6):
            c_ob = candles[i]
            if c_ob.close >= c_ob.open:  # Должна быть медвежья (перед бычьим импульсом)
                continue
            ob_lo, ob_hi = c_ob.low, c_ob.high
            end = min(i + 5, len(candles) - 1)
            hi_after = max(candles[j].high for j in range(i + 1, end + 1))
            if (hi_after - ob_hi) / ob_hi * 100 < 1.5:
                continue
            broken = any(candles[j].low < ob_lo for j in range(i + 2, len(candles) - 1))
            if not broken:
                continue
            break_zones.append((ob_lo, ob_hi))

        if not break_zones:
            return None

        # ── Ищем пересечение ──────────────────────────────────────────────
        best = None
        for flo, fhi in fvg_zones:
            for blo, bhi in break_zones:
                overlap_lo = max(flo, blo)
                overlap_hi = min(fhi, bhi)
                if overlap_hi <= overlap_lo:
                    continue
                if current_price < overlap_lo * 0.99 or current_price > overlap_hi * 1.01:
                    continue
                overlap_pct = (overlap_hi - overlap_lo) / current_price * 100
                if overlap_pct < 0.05:
                    continue
                dist = abs((overlap_lo + overlap_hi) / 2 - current_price)
                if best is None or dist < best[0]:
                    best = (dist, overlap_lo, overlap_hi, flo, fhi, blo, bhi, overlap_pct)

        if best is None:
            return None

        _, ov_lo, ov_hi, flo, fhi, blo, bhi, ov_pct = best
        bonus = min(30, int(24 + ov_pct * 3))
        return PatternResult(
            name="ICT_UNICORN_SHORT", score_bonus=bonus,
            confidence=min(0.88, 0.72 + ov_pct * 0.05),
            direction="short",
            suggested_sl_pct=round((ov_hi * 1.007 - current_price) / current_price * 100 + 0.3, 2),
            reasons=[
                f"🦄 ICT UNICORN SHORT | Overlap [{ov_lo:.6f}–{ov_hi:.6f}]",
                f"Bearish FVG [{flo:.6f}–{fhi:.6f}] ∩ Breaker [{blo:.6f}–{bhi:.6f}]",
                "FVG + Breaker Block в одной зоне = institutional confluence 75%+",
            ],
        )

    def detect_breaker_short(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """BREAKER_SHORT: Пробитый бычий OB → зона сопротивления (ICT Bearish Breaker Block).
        1. Bullish OB = медвежья свеча перед бычьим импульсом
        2. Цена пробивает OB вниз → OB флипает в сопротивление
        3. Цена возвращается в бывшую OB зону → SHORT вход
        """
        if len(candles) < 15:
            return None
        atr_v = _atr(candles, 14)
        if atr_v <= 0:
            return None

        current_price = candles[-1].close
        lookback = min(50, len(candles) - 5)
        best = None

        for i in range(len(candles) - lookback, len(candles) - 6):
            c_ob = candles[i]

            # OB candle: последняя медвежья свеча перед бычьим импульсом
            if c_ob.close >= c_ob.open:
                continue
            ob_low  = c_ob.low
            ob_high = c_ob.high

            # Бычий импульс после OB (≥1.5% роста)
            end = min(i + 5, len(candles) - 1)
            hi_after = max(candles[j].high for j in range(i + 1, end + 1))
            rise_pct = (hi_after - ob_high) / ob_high * 100
            if rise_pct < 1.5:
                continue

            # Цена пробивает OB вниз (проходит ниже ob_low)
            broken_idx = -1
            for j in range(i + 2, len(candles) - 1):
                if candles[j].low < ob_low:
                    broken_idx = j
                    break
            if broken_idx < 0:
                continue

            # Цена возвращается В зону OB (retest бывшей поддержки = теперь сопротивление)
            if current_price < ob_low * 0.99:
                continue
            if current_price > ob_high * 1.015:
                continue

            # Хотя бы одна свеча после пробоя вернулась в зону
            retest = any(candles[j].high >= ob_low for j in range(broken_idx + 1, len(candles)))
            if not retest:
                continue

            gap_pct = (ob_high - ob_low) / current_price * 100
            dist = abs((ob_low + ob_high) / 2 - current_price)
            if best is None or dist < best[0]:
                best = (dist, ob_low, ob_high, rise_pct, gap_pct)

        if best is None:
            return None

        _, ob_low, ob_high, rise_pct, gap_pct = best
        bonus = min(26, int(18 + min(rise_pct, 10) * 0.5 + gap_pct * 2))
        return PatternResult(
            name="BREAKER_SHORT", score_bonus=bonus,
            confidence=min(0.82, 0.62 + min(rise_pct, 15) * 0.01),
            direction="short",
            suggested_sl_pct=round((ob_high * 1.005 - current_price) / current_price * 100 + 0.2, 2),
            reasons=[
                f"🔴 Bearish Breaker [{ob_low:.6f} – {ob_high:.6f}]",
                f"Бычий OB +{rise_pct:.1f}% пробит вниз → retest = сопротивление",
            ],
        )

    def detect_fvg_short(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """FVG_SHORT: Bearish Fair Value Gap — цена в зоне незаполненного гэпа (ICT сопротивление).
        3 свечи: c1 | c2(сильный медвежий импульс) | c3. Гэп = c3.high < c1.low.
        Вход когда цена возвращается в гэп (retest зоны сопротивления).
        """
        if len(candles) < 10:
            return None
        atr_v = _atr(candles, 14)
        if atr_v <= 0:
            return None

        current_price = candles[-1].close
        lookback = min(20, len(candles) - 2)
        best = None

        for i in range(len(candles) - lookback, len(candles) - 2):
            c1 = candles[i]
            c2 = candles[i + 1]
            c3 = candles[i + 2]

            # Средняя свеча — сильный медвежий импульс
            if c2.close >= c2.open:
                continue
            body2 = c2.open - c2.close
            if body2 < atr_v * 1.2:
                continue

            # FVG: гэп между c3.high и c1.low (медвежий разрыв)
            fvg_lo = c3.high
            fvg_hi = c1.low
            if fvg_hi <= fvg_lo:
                continue

            gap_pct = (fvg_hi - fvg_lo) / c2.close * 100
            if gap_pct < 0.1:
                continue

            # Гэп ещё не заполнен (цена не вернулась выше c1.low)
            filled = any(candles[j].high >= fvg_hi for j in range(i + 3, len(candles)))
            if filled:
                continue

            # Цена в зоне гэпа или подходит снизу (буфер 2%)
            if current_price > fvg_hi * 1.02:
                continue
            if current_price < fvg_lo * 0.995:
                continue

            dist = abs((fvg_lo + fvg_hi) / 2 - current_price)
            if best is None or dist < best[0]:
                best = (dist, fvg_lo, fvg_hi, gap_pct, body2)

        if best is None:
            return None

        _, fvg_lo, fvg_hi, gap_pct, body2 = best
        bonus = min(22, int(14 + gap_pct * 4))
        return PatternResult(
            name="FVG_SHORT", score_bonus=bonus,
            confidence=min(0.80, 0.58 + gap_pct * 0.08),
            direction="short",
            suggested_sl_pct=round((fvg_hi * 1.005 - current_price) / current_price * 100 + 0.2, 2),
            reasons=[
                f"📊 Bearish FVG [{fvg_lo:.6f} – {fvg_hi:.6f}] ({gap_pct:.2f}%)",
                f"Импульс {body2 / atr_v:.1f}×ATR | Незаполненный гэп = сопротивление",
            ],
        )

    # ── НОВЫЕ SHORT ПАТТЕРНЫ ──────────────────────────────────────────────────

    def detect_breakout_short(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """BREAKOUT_SHORT: Пробой флэта вниз с объёмом."""
        if len(candles) < 25:
            return None
        cons      = candles[-20:-2]
        last      = candles[-1]
        low_cons  = min(c.low  for c in cons)
        high_cons = max(c.high for c in cons)
        r_pct     = (high_cons - low_cons) / low_cons * 100 if low_cons else 999
        if r_pct > 3.0 or last.close >= low_cons:
            return None
        vol_spike = _vol_spike(candles, 20)
        if vol_spike < 1.5:
            return None
        rng = last.high - last.low
        if rng > 0 and (last.high - last.close) / rng < 0.5:
            return None
        breakdown_pct = (low_cons - last.close) / low_cons * 100
        bonus = min(25, int(10 + vol_spike * 3 + breakdown_pct * 2))
        return PatternResult(
            name="BREAKOUT_SHORT", score_bonus=bonus,
            confidence=min(0.9, 0.5 + vol_spike * 0.1), direction="short",
            suggested_sl_pct=round((high_cons - last.close) / last.close * 100, 2),
            reasons=[f"Пробой ниже {low_cons:.4f} (флэт {r_pct:.1f}%)",
                     f"Volume spike {vol_spike:.1f}x | -{breakdown_pct:.2f}%"],
        )

    def detect_momentum_short(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """MOMENTUM_SHORT: Медвежья импульсная свеча + volume spike ≥2x."""
        if len(candles) < 25:
            return None
        last      = candles[-1]
        atr_v     = _atr(candles, 14)
        vol_spike = _vol_spike(candles, 20)
        if vol_spike < 2.0:
            return None
        rng = last.high - last.low
        if rng <= 0 or (last.high - last.close) / rng < 0.65:
            return None
        body = last.open - last.close
        if body <= 0 or (atr_v > 0 and body < atr_v * 0.5):
            return None
        closes = _closes(candles)
        ema20  = _ema(closes, 20)
        if ema20 and last.close > ema20[-1]:
            return None
        rsi = getattr(md, "rsi_1h", None) if md else None
        if rsi and (rsi > 65 or rsi < 25):
            return None
        bonus = min(20, int(12 + vol_spike * 1.5))
        return PatternResult(
            name="MOMENTUM_SHORT", score_bonus=bonus,
            confidence=min(0.85, 0.55 + vol_spike * 0.08), direction="short",
            suggested_sl_pct=round((last.open - last.close) / last.close * 100, 2),
            reasons=[f"Медвежий импульс | Volume {vol_spike:.1f}x avg"],
        )

    def detect_liquidity_sweep_short(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """LIQUIDITY_SWEEP_SHORT: Stop hunt вверх → разворот вниз (ICT/SMC).
        
        v2: sweep.close может быть чуть выше recent_sh (буфер 0.5%) —
        на криптоалтах свеча часто закрывается в зоне swept highs, а не ниже.
        """
        if len(candles) < 20:
            return None
        last  = candles[-1]
        sweep = candles[-2]
        atr_v = _atr(candles[:-2], 14)
        if atr_v <= 0:
            return None
        upper_wick = sweep.high - max(sweep.open, sweep.close)
        if upper_wick < atr_v * 1.0:
            return None
        swing_highs_list = _swing_highs(candles[:-3], lookback=3)
        if not swing_highs_list:
            return None
        recent_sh = (max(swing_highs_list[-3:]) if len(swing_highs_list) >= 3
                     else swing_highs_list[-1])
        if sweep.high < recent_sh:
            return None
        # ✅ FIX v2: буфер 0.5% — sweep.close может чуть не дойти ниже recent_sh
        # (на памп-свечах закрытие часто остаётся в зоне swept high)
        if sweep.close > recent_sh * 1.005:
            return None
        if last.close >= last.open:
            return None
        vol_spike     = _vol_spike(candles[:-1], 20)
        sweep_height  = (sweep.high - recent_sh) / recent_sh * 100
        bonus         = min(25, int(18 + sweep_height * 2 + vol_spike))
        return PatternResult(
            name="LIQUIDITY_SWEEP_SHORT", score_bonus=bonus,
            confidence=0.75, direction="short",
            suggested_sl_pct=round((sweep.high - last.close) / last.close * 100 + 0.2, 2),
            reasons=[f"Ликвидность снята над {recent_sh:.4f}",
                     f"Sweep +{sweep_height:.2f}% выше свинг-хая | Volume {vol_spike:.1f}x",
                     "Разворот вниз подтверждён"],
        )

    def detect_pump_dump_short(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """
        🆕 PUMP_DUMP_SHORT: Резкий pump (+50-200%) → откат → вход в шорт.
        
        Как у DUMP Signals бота:
        1. Резкий pump за 1-3 дня (+50%+)
        2. Вершина сформирована (откат 5-20% от максимума)
        3. Возвращение к зоне вершины для входа в шорт
        4. Ожидаемый dump: -30% до -70%
        """
        if len(candles) < 100:  # Минимум ~1.5 дня на 15m
            return None
        
        # Анализируем последние 3 дня (288 свечей 15m)
        lookback = min(288, len(candles) - 10)
        recent = candles[-lookback:]
        
        # Ищем максимум и минимум за период
        max_high = max(c.high for c in recent)
        min_low = min(c.low for c in recent)
        
        # Проверяем был ли pump
        pump_pct = (max_high - min_low) / min_low * 100 if min_low else 0
        
        # Минимальный pump: +50% (как у DUMP Signals)
        if pump_pct < 50:
            return None
        
        last = candles[-1]
        current_price = last.close
        
        # Проверяем откат от вершины (5-30% — зона входа)
        pullback_pct = (max_high - current_price) / max_high * 100 if max_high else 0
        
        # Слишком рано (< 5% отката) или слишком поздно (> 35% — dump уже начался)
        if pullback_pct < 5 or pullback_pct > 35:
            return None
        
        # Проверяем объём (должен быть высоким)
        vol_spike = _vol_spike(candles, 20)
        if vol_spike < 1.5:
            return None
        
        # Проверяем текущую свечу — должна быть медвежьей или с верхним wick
        upper_wick = last.high - max(last.open, last.close)
        body_size = abs(last.close - last.open)
        
        is_bearish = last.close < last.open
        has_rejection = upper_wick > body_size * 0.8
        
        if not (is_bearish or has_rejection):
            return None
        
        # Бонус зависит от размера pump и качества отката
        bonus = min(30, int(20 + pump_pct * 0.1 + pullback_pct * 0.3))
        
        return PatternResult(
            name="PUMP_DUMP_SHORT",
            score_bonus=bonus,
            confidence=min(0.85, 0.6 + pump_pct / 200),
            direction="short",
            suggested_sl_pct=round((max_high - current_price) / current_price * 100 + 2.0, 2),
            reasons=[
                f"🚀 Pump: +{pump_pct:.0f}% за 3 дня",
                f"📉 Откат: {pullback_pct:.1f}% от вершины",
                f"🎯 SHORT-зона: ${current_price:.6f}+",
                f"Ожидаемый dump: -30% до -70%",
            ],
        )

    def detect_distribution_break(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """DISTRIBUTION_BREAK: Пробой нижней границы зоны распределения."""
        if len(candles) < 25:
            return None
        dist  = candles[-20:-2]
        last  = candles[-1]
        l     = min(c.low  for c in dist)
        h     = max(c.high for c in dist)
        r_pct = (h - l) / l * 100 if l else 999
        if r_pct > 3.0 or r_pct < 0.5 or last.close >= l:
            return None
        vol_spike     = _vol_spike(candles, 20)
        if vol_spike < 1.3:
            return None
        breakdown_pct = (l - last.close) / l * 100
        bonus         = min(18, int(10 + vol_spike * 2 + breakdown_pct))
        return PatternResult(
            name="DISTRIBUTION_BREAK", score_bonus=bonus,
            confidence=0.65, direction="short",
            suggested_sl_pct=round((h - last.close) / last.close * 100, 2),
            reasons=[f"Пробой распределения ниже {l:.4f} | -{breakdown_pct:.2f}%",
                     f"Volume {vol_spike:.1f}x"],
        )

    def detect_wyckoff_upthrust(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        """WYCKOFF_UPTHRUST: Ложный пробой хая зоны распределения.
        
        v2: расширен диапазон 1.5-25%, ослаблен volume фильтр,
        допускается верхний wick на последней свече как подтверждение.
        """
        if len(candles) < 30:
            return None
        dist   = candles[-30:-5]
        last   = candles[-1]
        prev   = candles[-2]
        r_h    = max(c.high for c in dist)
        r_l    = min(c.low  for c in dist)
        r_pct  = (r_h - r_l) / r_l * 100 if r_l else 999
        # ✅ FIX v2: крипто-алты имеют диапазоны 10-25%
        if not (1.5 < r_pct < 25.0):
            return None
        if prev.high < r_h or prev.close > r_h:
            return None
        avg_vol = _avg_vol(dist, min(len(dist), 15))
        # ✅ FIX v2: расслабляем — памп на upthrust допустим
        if prev.quote_volume > avg_vol * 2.5:
            return None
        if last.close > r_h:
            return None
        # ✅ FIX v2: принимаем медвежью свечу ИЛИ свечу с большим верхним wick
        upper_wick_last = last.high - max(last.open, last.close)
        atr_v = _atr(candles[-15:], 14)
        is_rejecting = last.close < last.open or upper_wick_last > atr_v * 0.3
        if not is_rejecting:
            return None
        uth = (prev.high - r_h) / r_h * 100
        bonus = 22 if r_pct < 8.0 else 20
        return PatternResult(
            name="WYCKOFF_UPTHRUST", score_bonus=bonus,
            confidence=0.80, direction="short",
            suggested_sl_pct=round((prev.high - last.close) / last.close * 100 + 0.3, 2),
            reasons=[f"Wyckoff Upthrust: диапазон {r_pct:.1f}%",
                     f"Upthrust +{uth:.2f}% выше сопротивления",
                     "Объём в норме — ложный пробой"],
        )

    # ── КЛАССИЧЕСКИЕ ПАТТЕРНЫ ─────────────────────────────────────────────────

    def detect_mega_short(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        if len(candles) < 21:
            return None
        last       = candles[-2]  # закрытая свеча — стабильный сигнал
        upper_wick = last.high - max(last.open, last.close)
        body       = _body(last)
        if upper_wick < body * 1.5:
            return None
        return PatternResult(
            name="MEGA_SHORT", score_bonus=20, confidence=0.6, direction="short",
            reasons=["Верхний wick большой", "Отскок от сопротивления"],
        )

    def detect_trap_long(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        if len(candles) < 20:
            return None
        last  = candles[-1]
        prev  = candles[-2]
        atr_v = _atr(candles[:-2], 14)
        upper_wick = prev.high - max(prev.open, prev.close)
        if upper_wick < atr_v * 0.8 or last.close >= last.open:
            return None
        return PatternResult(
            name="TRAP_LONG", score_bonus=18, confidence=0.58, direction="short",
            reasons=["Лонгисты пойманы в ловушку", "Разворот вниз подтверждён"],
        )

    def detect_rejection_short(self, candles, hourly_deltas=None, md=None) -> Optional[PatternResult]:
        if len(candles) < 11:
            return None
        last       = candles[-2]  # закрытая свеча — стабильный сигнал
        upper_wick = last.high - max(last.open, last.close)
        body       = _body(last)
        if upper_wick < body * 1.0 or last.close > last.open:
            return None
        return PatternResult(
            name="REJECTION_SHORT", score_bonus=15, confidence=0.55, direction="short",
            reasons=["Отскок от сопротивления", f"Upper wick {upper_wick:.4f}"],
        )
