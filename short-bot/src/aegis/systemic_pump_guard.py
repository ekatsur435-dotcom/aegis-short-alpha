"""
Aegis SystemicPumpGuard — D
Блокирует SHORT позиции при системном памп рынка:
  - BTC растёт >= SYSTEMIC_PUMP_BTC_PCT%/час (default +3%)
  - >50% символов с price_trend=up (alts breadth)
Блокирует только SHORT на символах с HTF=BULLISH (локально, попарно).
"""
from __future__ import annotations
import os
import logging
from datetime import datetime, timedelta

logger = logging.getLogger("aegis.systemic_pump_guard")

_PUMP_BTC_PCT    = float(os.getenv("SYSTEMIC_PUMP_BTC_PCT",    "3.0"))
_PUMP_ALTS_RATIO = float(os.getenv("SYSTEMIC_PUMP_ALTS_RATIO", "0.50"))
_PUMP_COOLDOWN_M = int(os.getenv("SYSTEMIC_PUMP_COOLDOWN_MIN", "30"))


class SystemicPumpGuard:
    """
    Обновляется каждым scan_symbol через update_symbol(price_trend).
    В начале скан-цикла вызывать reset_cycle().
    is_pump() -> True если зафиксирован системный памп рынка.
    Блокирует SHORT только на символах с HTF=BULLISH.
    """

    def __init__(self):
        self._btc_change_1h: float           = 0.0
        self._cycle_up:      int             = 0
        self._cycle_total:   int             = 0
        self._pump_until:    datetime | None = None
        self._last_reason:   str             = ""

    def reset_cycle(self):
        self._cycle_up    = 0
        self._cycle_total = 0

    def update_btc(self, btc_change_1h: float):
        self._btc_change_1h = btc_change_1h

    def update_symbol(self, price_trend: str):
        self._cycle_total += 1
        if price_trend == "up":
            self._cycle_up += 1

    def evaluate(self):
        btc_pump   = self._btc_change_1h >= _PUMP_BTC_PCT
        alts_ratio = self._cycle_up / self._cycle_total if self._cycle_total > 0 else 0.0
        alts_pump  = alts_ratio >= _PUMP_ALTS_RATIO

        if btc_pump and alts_pump:
            self._pump_until = datetime.utcnow() + timedelta(minutes=_PUMP_COOLDOWN_M)
            self._last_reason = (
                f"BTC {self._btc_change_1h:+.1f}%/1H + {alts_ratio:.0%} альтов растут"
            )
            logger.warning(
                f"[SYSTEMIC PUMP] {self._last_reason}. "
                f"SHORT на HTF=BULLISH заблокирован до {self._pump_until.strftime('%H:%M')} UTC"
            )

    def is_pump(self) -> bool:
        if self._pump_until is None:
            return False
        if datetime.utcnow() < self._pump_until:
            return True
        self._pump_until = None
        return False

    @property
    def reason(self) -> str:
        return self._last_reason
