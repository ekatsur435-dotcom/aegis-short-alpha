"""
🔴 AEGIS SHORT ALPHA v1.0 — Institutional Short Trading Bot
FastAPI Application

УЛУЧШЕНИЯ vs short-bot v2.3:
  ✅ AegisSignalEngine — взвешенный 5-компонентный скоринг
  ✅ PumpDetector — Z-Score + VWAP exhaustion
  ✅ OIAnalyzer — полный OI + Funding анализ
  ✅ LiquidationMapper — кластеры ликвидаций
  ✅ DeltaAnalyzer — order flow CVD
  ✅ SmartDCAEngine — ATR-based dynamic grid
  ✅ AegisRiskManager — Kelly Criterion + Circuit Breakers
  ✅ PerformanceTracker — real-time P&L analytics
  ✅ Paid tier: 150 пар, 180s scan, 15 позиций
  ✅ Redis batch оптимизация
  ✅ Новые Telegram команды: /risk, /dca, /perf, /components
"""

import os
import re
import asyncio
import sys
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from contextlib import asynccontextmanager
from decimal import Decimal

# Настройка логирования — INFO уровень для видимости fallback логов
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
import uvicorn


# ============================================================================
# PATH SETUP — shared modules
# ============================================================================

def _find_shared() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "shared"),
        os.path.join(here, "..", "shared"),
        os.path.join(here, "..", "..", "shared"),
        os.path.join(here, "..", "..", "..", "shared"),
        "/opt/render/project/src/shared",
    ]
    for c in candidates:
        c = os.path.normpath(c)
        if os.path.isdir(c):
            return c
    return os.path.join(here, "..", "..", "shared")


_SHARED = _find_shared()
_SRC    = os.path.dirname(os.path.abspath(__file__))
# ВАЖНО: _SHARED должен быть в sys.path РАНЬШЕ _SRC, иначе пустой
# short-bot/src/execution/ затенит shared/execution/ (package shadowing bug)
for _p in [_SHARED, os.path.dirname(_SHARED)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
# _SRC добавляем в конец — локальные модули (aegis/, detectors/) не конфликтуют с shared
if _SRC not in sys.path:
    sys.path.append(_SRC)

print(f"📁 shared: {_SHARED} | src: {_SRC}")

# ── Shared modules (existing) ──
from upstash.redis_client import get_redis_client
from utils.binance_client import get_binance_client
from core.scorer import get_short_scorer, reset_scorers
from core.pattern_detector import ShortPatternDetector
from core.position_tracker import PositionTracker
from core.short_filter import get_short_filter, get_short_tp_config
from core.realtime_scorer import get_realtime_scorer
from core.consolidation_detector import ConsolidationDetector, filter_mid_range
from bot.telegram import TelegramBot, TelegramCommandHandler
from utils.okx_liquidation_ws import OKXLiquidationFeed

# ── Aegis modules (NEW) ──
from aegis.signal_engine import AegisSignalEngine, SignalStrength
from aegis.smart_dca import SmartDCAEngine, GridConfig, GridType
from aegis.risk_manager import AegisRiskManager, RiskLimits
from aegis.performance_tracker import PerformanceTracker, TradeRecord
from detectors.pump_detector import PumpDetector, ZScoreConfig
from detectors.oi_analyzer import OIAnalyzer, FundingConfig
from detectors.liquidation_mapper import LiquidationMapper
from detectors.delta_analyzer import DeltaAnalyzer


# ============================================================================
# CONFIGURATION — PAID MINIMAL TIER
# ============================================================================

class Config:
    """Aegis Configuration — Paid Minimal Tier"""
    BOT_NAME    = "Aegis-Short-Alpha"
    BOT_VERSION = "1.0.0"
    BOT_TYPE    = "short"

    # ── Paid tier limits (vs free 50/300/10) ──
    MAX_PAIRS     = int(os.getenv("MAX_PAIRS", "150"))
    SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "180"))
    MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "12"))

    MIN_SCORE     = int(os.getenv("MIN_SHORT_SCORE", "55"))  # Снижено с 60: новая blend-формула (70% base) даёт ~55-65 при base=60
    SL_BUFFER     = float(os.getenv("SHORT_SL_BUFFER", "2.0"))  # ✅ FIX v17: 2.5→2.0% для RR≥1.5
    LEVERAGE      = os.getenv("SHORT_LEVERAGE", "5-30")

    # TP Config
    TP_LEVELS  = [2.5, 4.0, 6.5, 9.0, 12.0, 17.0]
    TP_WEIGHTS = [15,  20,  20,  15,  15,    15]

    # Risk management
    RISK_PER_TRADE      = float(os.getenv("RISK_PER_TRADE", "0.0004"))    # 0.1%
    MAX_POSITION_PCT    = float(os.getenv("MAX_POSITION_PCT", "0.15"))   # 15%
    MAX_EXPOSURE_PCT    = float(os.getenv("MAX_EXPOSURE_PCT", "0.60"))   # 60%
    DAILY_DD_LIMIT      = float(os.getenv("DAILY_DRAWDOWN_LIMIT", "3.0"))
    MAX_CONSEC_LOSS     = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
    KELLY_FRACTION      = float(os.getenv("KELLY_FRACTION", "0.25"))

    # Smart DCA
    DCA_LEVELS      = int(os.getenv("DCA_LEVELS", "4"))
    DCA_ATR_MULT    = float(os.getenv("DCA_ATR_MULT", "1.5"))
    DCA_SIZE_MULT   = float(os.getenv("DCA_SIZE_MULT", "1.5"))

    # Feature flags
    ENABLE_PUMP_DETECTOR     = os.getenv("ENABLE_PUMP_DETECTOR", "true").lower() == "true"
    ENABLE_OI_ANALYZER       = os.getenv("ENABLE_OI_ANALYZER", "true").lower() == "true"
    ENABLE_LIQ_MAPPER        = os.getenv("ENABLE_LIQ_MAPPER", "true").lower() == "true"
    ENABLE_DELTA             = os.getenv("ENABLE_DELTA", "true").lower() == "true"
    ENABLE_SMC               = os.getenv("USE_SMC", "true").lower() == "true"
    ENABLE_AEGIS_ENGINE      = os.getenv("ENABLE_AEGIS_ENGINE", "true").lower() == "true"
    ENABLE_SMART_DCA         = os.getenv("ENABLE_SMART_DCA", "true").lower() == "true"

    # Auto trading
    AUTO_TRADING    = os.getenv("AUTO_TRADING_ENABLED", "false").lower() == "true"
    # DEMO / REAL режим: BINGX_DEMO_MODE=true (демо, по умолч.) | BINGX_DEMO_MODE=false (реальные деньги ⚠️)
    BINGX_DEMO      = os.getenv("BINGX_DEMO_MODE", "true").strip().lower() not in ("false", "0", "no", "real")

    # Watchlist
    MIN_VOLUME_USDT     = int(os.getenv("MIN_VOLUME_USDT", "200000"))    # ✅ v2.1: 300K→200K
    MAX_WATCHLIST       = int(os.getenv("MAX_WATCHLIST", "200"))          # ✅ v2.1: 150→200
    WATCHLIST_REFRESH_H = float(os.getenv("WATCHLIST_REFRESH_H", "2.0")) # ✅ v2.1: обновление каждые 2ч

    # ATR-dynamic SL (M1)
    USE_ATR_SL  = os.getenv("USE_ATR_SL", "true").lower() == "true"
    ATR_SL_MULT = float(os.getenv("ATR_SL_MULT", "1.5"))
    ATR_SL_MIN  = float(os.getenv("ATR_SL_MIN_PCT", "1.0"))
    ATR_SL_MAX  = float(os.getenv("ATR_SL_MAX_PCT", "4.0"))

    # ✅ FIX: AEGIS_SHORT_MIN_SCORE — реальный порог Aegis engine (аналог LONG бота)
    AEGIS_MIN_SCORE      = int(os.getenv("AEGIS_SHORT_MIN_SCORE", "55"))
    # ✅ FIX: Adaptive threshold ceiling — max +N от MIN_SHORT_BASE_SCORE
    ADAPTIVE_MAX_BOOST   = int(os.getenv("ADAPTIVE_MAX_BOOST", "3"))
    # ✅ FIX: MOMENTUM SHORT порог (аналог LONG бота)
    MOMENTUM_SCORE_THRESHOLD = int(os.getenv("MOMENTUM_SCORE_THRESHOLD", "58"))

    # ✅ Постоянный блэклист — символы которые всегда пропускаем
    # Формат ENV: SYMBOL_BLACKLIST=GIGAUSDT,LUNAUSDT,我踏马来了USDT
    SYMBOL_BLACKLIST: set = set(
        s.strip().upper() for s in os.getenv("SYMBOL_BLACKLIST", "").split(",") if s.strip()
    )

    # Signals
    MAX_DAILY_TRADES  = int(os.getenv("MAX_DAILY_TRADES_SHORT", "10"))  # v3.0
    SIGNAL_TTL_HOURS = 24
    TRAIL_ACTIVATION = float(os.getenv("SHORT_TRAIL_ACTIVATION", "0.010"))

    # P4: Funding extreme thresholds (informational — scorer reads ENV directly)
    FUNDING_EXTREME_LONG  = float(os.getenv("FUNDING_EXTREME_LONG",  "-0.05"))
    FUNDING_EXTREME_SHORT = float(os.getenv("FUNDING_EXTREME_SHORT",  "0.05"))

    # P1: Order book
    ENABLE_ORDERBOOK = os.getenv("ENABLE_ORDERBOOK_SCORER", "true").lower() == "true"


# ============================================================================
# GLOBAL STATE
# ============================================================================

class BotState:
    def __init__(self):
        # Existing
        self.is_running       = False
        self.is_paused        = False
        self.last_scan        = None
        self.active_signals   = 0
        self.daily_signals    = 0
        self.watchlist: List[str] = []
        self.redis            = None
        self.binance          = None
        self.telegram         = None
        self.cmd_handler      = None
        self.auto_trader      = None
        self.tracker: Optional[PositionTracker] = None
        self.start_time       = None
        self._min_score       = Config.MIN_SCORE

        # Existing detectors (shared/core)
        self.scorer           = None
        self.pattern_detector = None
        self.consolidation_detector: Optional[ConsolidationDetector] = None  # 🆕

        # ── Aegis modules (NEW) ──
        self.signal_engine:       Optional[AegisSignalEngine]    = None
        self.dca_engine:          Optional[SmartDCAEngine]        = None
        self.risk_manager:        Optional[AegisRiskManager]      = None
        self.performance_tracker: Optional[PerformanceTracker]    = None

        # Detectors
        self.pump_detector:   Optional[PumpDetector]       = None
        self.oi_analyzer:     Optional[OIAnalyzer]         = None
        self.liq_mapper:      Optional[LiquidationMapper]  = None
        self.delta_analyzer:  Optional[DeltaAnalyzer]      = None
        self.okx_ws_feed:     Optional[OKXLiquidationFeed] = None

        # Metrics
        self.coinglass        = None
        self.fear_greed_index: Optional[int] = None   # 🆕 0-100, None = не загружен


state = BotState()


# ============================================================================
# WATCHLIST
# ============================================================================

async def _build_combined_watchlist(binance_client, min_vol: float, max_count: int) -> List[str]:
    from utils.binance_client import FALLBACK_WATCHLIST
    bybit_syms, binance_syms = set(), set()

    try:
        await binance_client._init_source()
    except Exception as e:
        print(f"⚠️ _init_source: {e}")

    try:
        result = await binance_client._bybit("/v5/market/tickers", {"category": "linear"})
        if result and result.get("list"):
            EXCLUDE = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S")
            for t in result.get("list", []):
                sym = t.get("symbol", "")
                if not sym.endswith("USDT"): continue
                if any(sym.endswith(s) for s in EXCLUDE): continue
                if float(t.get("turnover24h", 0)) >= min_vol:
                    bybit_syms.add(sym)
        print(f"✅ Bybit: {len(bybit_syms)} symbols")
    except Exception as e:
        print(f"⚠️ Bybit watchlist: {e}")

    try:
        tickers = await binance_client._binance("/fapi/v1/ticker/24hr")
        if tickers:
            EXCLUDE = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S")
            for t in tickers:
                sym = t.get("symbol", "")
                if not sym.endswith("USDT"): continue
                if any(sym.endswith(s) for s in EXCLUDE): continue
                if float(t.get("quoteVolume", 0)) >= min_vol:
                    binance_syms.add(sym)
        print(f"✅ Binance: {len(binance_syms)} symbols")
    except Exception as e:
        print(f"⚠️ Binance watchlist: {e}")

    if not bybit_syms and not binance_syms:
        print("⚠️ Fallback watchlist")
        return FALLBACK_WATCHLIST[:max_count]

    both    = list(bybit_syms & binance_syms)
    only_one = [s for s in (bybit_syms | binance_syms) if s not in set(both)]
    combined = (both + only_one)[:max_count]
    # ✅ FIX: Reject garbage symbols (Chinese chars, non-ASCII, malformed)
    _VALID_SYM = re.compile(r'^[A-Z0-9]{2,20}USDT$')
    result = [s for s in combined if _VALID_SYM.match(s)]
    # ✅ FIX БАГ 3: Дедупликация — сохраняет порядок, убирает дубликаты
    result = list(dict.fromkeys(result))
    if len(result) < len(combined):
        print(f"⚠️ Filtered {len(combined) - len(result)} invalid/duplicate symbols from watchlist")
    # ✅ FIX: ENV-блэклист (SYMBOL_BLACKLIST=GIGAUSDT,LUNAUSDT,...)
    if Config.SYMBOL_BLACKLIST:
        before = len(result)
        result = [s for s in result if s not in Config.SYMBOL_BLACKLIST]
        print(f"🚫 ENV blacklist filtered {before - len(result)} symbols")
    print(f"📊 Watchlist: {len(result)} (both={len(both)})")
    return result


# ============================================================================
# LIFESPAN
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"🚀 Starting {Config.BOT_NAME} v{Config.BOT_VERSION}...")
    state.start_time = datetime.utcnow()

    # ── Redis ──
    state.redis   = get_redis_client()
    redis_ok      = state.redis.health_check()
    print(f"{'✅' if redis_ok else '❌'} Redis")

    # ── Market data ──
    state.binance = get_binance_client()
    try:
        await state.binance._init_source()
    except Exception as e:
        print(f"⚠️ Binance init failed (Bybit fallback): {e}")

    # ── OKX WebSocket Liquidation Feed ──────────────────────────────────
    # REST /api/v5/public/liquidation-orders мёртв с 2023.
    # WS стрим пишет ликвидации в Redis: okx:liq:{symbol} TTL=300s
    state.binance.set_redis(state.redis)   # привязываем Redis к binance client
    state.okx_ws_feed = OKXLiquidationFeed(redis_client=state.redis)
    await state.okx_ws_feed.start()
    print("✅ OKX WS liquidation feed started (Redis cache mode)")

    # ── Existing scorer + patterns ──
    # BASE_SCORER получает мягкий порог (50) — строгий порог у AEGIS (65)
    _short_base_min = int(os.getenv("MIN_SHORT_BASE_SCORE", "58"))  # ✅ OPT v19: 50→58 убрана dead zone 50-57
    state.scorer           = get_short_scorer(_short_base_min)
    print(
        f"📐 Score thresholds: BASE_SCORER={_short_base_min} | "
        f"AEGIS_ENGINE={Config.MIN_SCORE} | FINAL_FILTER={Config.MIN_SCORE}"
    )
    state.pattern_detector = ShortPatternDetector()
    
    # 🆕 Consolidation Detector — блокировка входов в середине диапазона
    state.consolidation_detector = ConsolidationDetector(
        lookback=20, max_range_pct=5.0, min_candles=10
    )

    # ── Aegis Detectors ──
    print("🔧 Initializing Aegis detectors...")

    state.pump_detector = PumpDetector(ZScoreConfig(
        threshold=2.5, volume_spike=2.5, rsi_overbought=73, lookback=20
    )) if Config.ENABLE_PUMP_DETECTOR else None

    state.oi_analyzer = OIAnalyzer(
        FundingConfig(lookback_hours=24, oi_change_threshold=10.0,
                      funding_threshold=0.03, funding_spike=0.10),
        binance_client=state.binance
    ) if Config.ENABLE_OI_ANALYZER else None

    state.liq_mapper     = LiquidationMapper() if Config.ENABLE_LIQ_MAPPER else None
    state.delta_analyzer = DeltaAnalyzer()     if Config.ENABLE_DELTA else None

    # ── Aegis Signal Engine ──
    state.signal_engine = AegisSignalEngine(
        pump_detector=state.pump_detector,
        oi_analyzer=state.oi_analyzer,
        liq_mapper=state.liq_mapper,
        delta_analyzer=state.delta_analyzer,
        min_score=Config.AEGIS_MIN_SCORE,  # ✅ FIX: теперь читает AEGIS_SHORT_MIN_SCORE (было MIN_SHORT_SCORE=55)
    ) if Config.ENABLE_AEGIS_ENGINE else None

    # ── Smart DCA ──
    state.dca_engine = SmartDCAEngine(GridConfig(
        grid_type=GridType.ATR_BASED,
        dca_levels=Config.DCA_LEVELS,
        atr_multiplier=Config.DCA_ATR_MULT,
        size_multiplier=Config.DCA_SIZE_MULT,
        max_exposure_pct=Config.MAX_EXPOSURE_PCT,
    )) if Config.ENABLE_SMART_DCA else None

    # ── Risk Manager ──
    account_capital = float(os.getenv("ACCOUNT_CAPITAL_USD", "1000"))
    state.risk_manager = AegisRiskManager(
        limits=RiskLimits(
            max_position_pct=Config.MAX_POSITION_PCT,
            max_total_exposure=Config.MAX_EXPOSURE_PCT,
            max_daily_drawdown=Config.DAILY_DD_LIMIT,
            max_consecutive_loss=Config.MAX_CONSEC_LOSS,
            kelly_fraction=Config.KELLY_FRACTION,
        ),
        capital=account_capital,
    )

    # ── Performance Tracker ──
    state.performance_tracker = PerformanceTracker(redis_client=state.redis)

    print(f"✅ Aegis Engine: {'ON' if state.signal_engine else 'OFF'} | "
          f"DCA: {'ON' if state.dca_engine else 'OFF'} | "
          f"Risk: ✅ | Perf: ✅")

    # ── Telegram ──
    state.telegram = TelegramBot(
        bot_token=os.getenv("SHORT_TELEGRAM_BOT_TOKEN") or os.getenv("TG_BOT_TOKEN"),
        chat_id=os.getenv("SHORT_TELEGRAM_CHAT_ID")    or os.getenv("TG_CHAT_ID"),
        topic_id=os.getenv("SHORT_TELEGRAM_TOPIC_ID"),
    )
    telegram_ok = await state.telegram.send_test_message()
    print(f"{'✅' if telegram_ok else '❌'} Telegram")

    state.cmd_handler = TelegramCommandHandler(
        bot=state.telegram, redis_client=state.redis,
        bot_state=state, bot_type=Config.BOT_TYPE,
        scan_callback=scan_market, config=Config,
    )

    render_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if render_url:
        ok = await state.telegram.setup_webhook(f"{render_url}/webhook")
        print(f"{'✅' if ok else '⚠️'} Webhook")

    # ── BingX AutoTrader ──
    print(f"🔧 AUTO_TRADING={Config.AUTO_TRADING} | DEMO={Config.BINGX_DEMO}")
    if Config.AUTO_TRADING and Config.BINGX_DEMO:
        print("⚠️  BINGX_DEMO_MODE=true — ордера идут на ДЕМО-счёт BingX!")
        print("⚠️  Для реальной торговли: BINGX_DEMO_MODE=false в переменных окружения")
    if Config.AUTO_TRADING and not Config.BINGX_DEMO:
        print("💰 REAL TRADING MODE — ордера идут на РЕАЛЬНЫЙ счёт BingX!")
    if Config.AUTO_TRADING:
        try:
            from api.bingx_client import BingXClient
            from shared.execution.auto_trader import AutoTrader, TradeConfig

            bingx = BingXClient(
                api_key=os.getenv("BINGX_API_KEY"),
                api_secret=os.getenv("BINGX_API_SECRET"),
                demo=Config.BINGX_DEMO,
            )
            if await bingx.test_connection():
                trade_cfg = TradeConfig(
                    enabled=True, demo_mode=Config.BINGX_DEMO,
                    max_positions=Config.MAX_POSITIONS,
                    risk_per_trade=Config.RISK_PER_TRADE,
                    min_score_for_trade=Config.MIN_SCORE,
                    max_daily_risk=Config.DAILY_DD_LIMIT,
                    max_daily_trades=Config.MAX_DAILY_TRADES,
                )
                state.auto_trader = AutoTrader(
                    bingx_client=bingx, config=trade_cfg, telegram=state.telegram
                )
                print(f"✅ BingX {'DEMO' if Config.BINGX_DEMO else 'REAL'} AutoTrader")
            else:
                print("❌ BingX connection failed")
        except Exception as e:
            import traceback
            print(f"❌ AutoTrader init: {e}")
            print(f"📋 Full traceback:\n{traceback.format_exc()}")

    # ── Watchlist ──
    try:
        state.watchlist = await _build_combined_watchlist(
            state.binance, Config.MIN_VOLUME_USDT, Config.MAX_WATCHLIST
        )
    except Exception as e:
        print(f"⚠️ Watchlist failed: {e}")
        state.watchlist = []

    state.is_running = True
    state.last_scan  = datetime.utcnow()

    # ── Position Tracker ──
    state.tracker = PositionTracker(
        bot_type=Config.BOT_TYPE, telegram=state.telegram,
        redis_client=state.redis, binance_client=state.binance,
        config=Config, auto_trader=state.auto_trader,
    )

    mode_str = "DEMO" if Config.BINGX_DEMO else "REAL"
    at_str   = f"✅ {mode_str}" if state.auto_trader else "❌ disabled"
    await state.telegram.send_message(
        f"🔴 <b>{Config.BOT_NAME} v{Config.BOT_VERSION} запущен</b>\n\n"
        f"📊 Watchlist: {len(state.watchlist)} монет\n"
        f"🛑 SL: {Config.SL_BUFFER}%  |  Score≥{Config.MIN_SCORE}\n"
        f"🤖 AutoTrader: {at_str}\n"
        f"⚙️ Risk: {Config.RISK_PER_TRADE*100:.2f}% | Scan: {Config.SCAN_INTERVAL}s\n"
        f"💎 Aegis Engine: {'✅' if state.signal_engine else '❌'}\n"
        f"📐 Smart DCA: {'✅' if state.dca_engine else '❌'}\n"
        f"🛡️ Risk Manager: ✅ Kelly={Config.KELLY_FRACTION}x | DD limit={Config.DAILY_DD_LIMIT}%"
    )

    asyncio.create_task(background_scanner())
    asyncio.create_task(state.tracker.run())
    asyncio.create_task(_daily_report_task())
    asyncio.create_task(_fear_greed_task())        # 🆕 F&G polling
    asyncio.create_task(_startup_sl_sync())        # 🚨 FIX: SL=0 bug sync
    asyncio.create_task(_watchlist_refresh_task()) # ✅ C3: watchlist auto-refresh

    yield

    state.is_running = False
    if state.okx_ws_feed:
        await state.okx_ws_feed.stop()
    if state.binance:
        await state.binance.close()
    if state.auto_trader:
        await state.auto_trader.bingx.close()
    print("👋 Aegis stopped")


app = FastAPI(lifespan=lifespan, title=f"{Config.BOT_NAME} v{Config.BOT_VERSION}")


# ============================================================================
# SCAN LOGIC
# ============================================================================

def _is_fresh(existing: List[Dict]) -> bool:
    if not existing or existing[0].get("status") != "active":
        return False
    try:
        age_h = (datetime.utcnow() -
                 datetime.fromisoformat(existing[0].get("timestamp", ""))
                 ).total_seconds() / 3600
        return age_h < Config.SIGNAL_TTL_HOURS
    except Exception:
        return True


def _ohlcv(candles) -> List[List[float]]:
    return [[c.open, c.high, c.low, c.close, c.volume] for c in candles]


async def _count_real_positions() -> int:
    if state.auto_trader:
        try:
            pos = await state.auto_trader.bingx.get_positions()
            short_pos = [p for p in pos if (
                getattr(p, "position_side", "").upper() == "SHORT" or
                getattr(p, "side", "").upper() == "SHORT"
            )]
            return len(short_pos)
        except Exception as e:
            print(f"[SHORT] count_positions error: {e}")
    # Fallback
    try:
        cutoff = datetime.utcnow() - timedelta(hours=Config.SIGNAL_TTL_HOURS)
        all_active = state.redis.get_active_signals(Config.BOT_TYPE)
        return sum(1 for s in all_active
                   if datetime.fromisoformat(s.get("timestamp", "2000-01-01")) > cutoff)
    except Exception:
        return 0


async def scan_symbol(symbol: str, cached_btc_1h: Optional[float] = None, verbose: bool = True) -> Optional[Dict]:
    """
    Aegis scan_symbol v1.0:
    - Параллельный расчёт через AegisSignalEngine
    - Существующие фильтры (ShortFilter, RealtimeScorer) сохранены
    - Smart DCA grid рассчитывается для каждого сигнала
    - VERBOSE LOGGING: показывает каждый этап скоринга
    """
    log_prefix = f"🔍 [{symbol}]"
    try:
        # ✅ OPT: Проверяем in-memory наборы (загружены в scan_market) — нет Redis-вызовов
        # Fallback на Redis.exists если наборы не инициализированы (первый запуск)
        _bl_set = getattr(state, '_blacklist_set', None)
        _sk_set = getattr(state, '_skip_nodata_set', None)
        if _bl_set is not None:
            if symbol in _bl_set: return None
        elif state.redis and state.redis.client.exists(f"blacklist:{symbol}"):
            return None
        if _sk_set is not None:
            if symbol in _sk_set: return None
        elif state.redis and state.redis.client.exists(f"skip:nodata:{symbol}"):
            return None

        md = await state.binance.get_complete_market_data(symbol)
        if not md:
            # П1: Попробовать альтернативный формат (SPACE-USDT для OKX)
            alt_symbol = symbol  # Binance/Bybit уже пробовали
            try:
                from api.okx_client import get_okx_client
                okx = get_okx_client()
                okx_data = await okx.get_open_interest(symbol)
                if okx_data:
                    pass  # Fallback частичный — продолжаем с Bybit-only
            except Exception:
                pass
            if verbose:
                print(f"{log_prefix} ❌ Нет market data от Binance/Bybit — пропуск")
            # ✅ FIX: Счётчик промахов → вечный бан после 3 раз
            try:
                if state.redis:
                    count_key = f"nodata_count:{symbol}"
                    count = state.redis.client.incr(count_key)
                    state.redis.client.expire(count_key, 86400 * 30)  # Счётчик живёт 30 дней
                    if count >= 3:
                        # Вечный бан — без TTL
                        state.redis.client.set(f"blacklist:{symbol}", f"nodata:{count}")
                        state.redis.client.delete(count_key)
                        print(f"🚫 [{symbol}] Добавлен в постоянный блэклист ({count} промахов)")
                        # Обновляем in-memory set
                        if hasattr(state, '_blacklist_set') and state._blacklist_set is not None:
                            state._blacklist_set.add(symbol)
                    else:
                        # skip TTL читается из ENV SKIP_NODATA_TTL (дефолт 86400 = 24ч)
                        _skip_ttl = int(os.getenv("SKIP_NODATA_TTL", "86400"))
                        state.redis.client.setex(f"skip:nodata:{symbol}", _skip_ttl, "1")
                        # Обновляем in-memory set
                        if hasattr(state, '_skip_nodata_set') and state._skip_nodata_set is not None:
                            state._skip_nodata_set.add(symbol)
            except Exception:
                pass
            return None

        # Загружаем OHLCV параллельно
        ohlcv_15m, ohlcv_30m, ohlcv_4h = await asyncio.gather(
            state.binance.get_klines(symbol, "15m", 100),
            state.binance.get_klines(symbol, "30m", 50),
            state.binance.get_klines(symbol, "4h", 20),
            return_exceptions=True,
        )
        if isinstance(ohlcv_15m, Exception) or not ohlcv_15m or len(ohlcv_15m) < 20:
            if verbose:
                print(f"{log_prefix} ❌ Недостаточно OHLCV данных (нужно 20, есть {len(ohlcv_15m) if ohlcv_15m else 0})")
            return None
        if isinstance(ohlcv_30m, Exception): ohlcv_30m = []
        if isinstance(ohlcv_4h, Exception):  ohlcv_4h  = []

        # ── П10: Multi-Timeframe RSI bonus ──────────────────────────────
        _mtf_bonus = 0
        try:
            def _calc_rsi(candles, period=14):
                if not candles or len(candles) < period + 1:
                    return None
                closes = [c.close for c in candles]
                gains, losses = [], []
                for i in range(1, len(closes)):
                    d = closes[i] - closes[i-1]
                    gains.append(max(d, 0)); losses.append(max(-d, 0))
                avg_g = sum(gains[:period]) / period
                avg_l = sum(losses[:period]) / period
                for i in range(period, len(gains)):
                    avg_g = (avg_g * (period-1) + gains[i]) / period
                    avg_l = (avg_l * (period-1) + losses[i]) / period
                rs = avg_g / avg_l if avg_l > 0 else 100
                return round(100 - 100 / (1 + rs), 1)

            rsi_30m = _calc_rsi(ohlcv_30m) if ohlcv_30m else None
            rsi_4h  = _calc_rsi(ohlcv_4h)  if ohlcv_4h  else None
            rsi_1h  = md.rsi_1h or 50
            _p1h = getattr(md, "price_change_1h", 0) or 0
            _p4h = getattr(md, "price_change_4h", 0) or 0
            # Пороги читаются из ENV (были хардкод -3.0 / -8.0)
            _momentum_1h_thr = float(os.getenv("MOMENTUM_DOWNTREND_1H", "-3.0"))
            _momentum_4h_thr = float(os.getenv("MOMENTUM_DOWNTREND_4H", "-8.0"))
            _is_downtrend = _p1h < _momentum_1h_thr or _p4h < _momentum_4h_thr

            # Шорт: все таймфреймы перегреты → сильный сигнал
            if rsi_4h and rsi_30m:
                if rsi_4h > 70 and rsi_1h > 65 and rsi_30m > 65:
                    _mtf_bonus = 15
                    if verbose: print(f"{log_prefix} 📈 [MTF] RSI 4H={rsi_4h} 1H={rsi_1h:.0f} 30M={rsi_30m} — всё перегрето +15")
                elif rsi_4h > 65 and rsi_1h > 60:
                    _mtf_bonus = 8
                    if verbose: print(f"{log_prefix} 📈 [MTF] RSI 4H={rsi_4h} 1H={rsi_1h:.0f} — перегрев +8")
                elif rsi_4h < 40 and rsi_1h < 45 and _is_downtrend:
                    # MOMENTUM SHORT: RSI низкий, цена падает → trend continuation SHORT
                    _mtf_bonus = 5
                    if verbose: print(f"{log_prefix} 📉 [MTF] RSI {rsi_1h:.0f} низкий но DOWNTREND {_p1h:.1f}%/1H — SHORT продолжение +5")
                elif rsi_4h < 40 and rsi_1h < 45:
                    _mtf_bonus = -5  # Перепродан без ценового подтверждения — лёгкий штраф
                    if verbose: print(f"{log_prefix} ⚠️ [MTF] RSI 4H={rsi_4h} перепродан без тренда {_mtf_bonus}")
            elif rsi_4h:
                if rsi_4h > 70: _mtf_bonus = 8
                elif rsi_4h > 65: _mtf_bonus = 4
        except Exception as _mtf_e:
            if verbose: print(f"{log_prefix} ⚠️ [MTF] error: {_mtf_e}")
        # ────────────────────────────────────────────────────────────────

        # ── Существующий базовый scorer (keep backward compat) ──
        hourly_deltas = await state.binance.get_hourly_volume_profile(symbol, 7)
        price_trend   = state.pattern_detector._get_price_trend(ohlcv_15m)
        patterns      = state.pattern_detector.detect_all(ohlcv_15m, hourly_deltas, md)

        # 🆕 Паттерны на 30M — уже загружены, просто запускаем detect_all
        # Вес 1.15x (между 15m и 4H) — более структурные чем 15m
        if ohlcv_30m and len(ohlcv_30m) >= 20:
            try:
                _pat_30m = state.pattern_detector.detect_all(ohlcv_30m, None, md)
                for _p in _pat_30m:
                    _p.score_bonus = int(_p.score_bonus * 1.15)
                    _p.name = f"{_p.name}_30M"
                patterns = patterns + _pat_30m
            except Exception:
                pass

        # ✅ v18: HTF паттерны на 4H и 1D (более значимые сигналы)
        # Паттерн на 4H весит больше т.к. структурно значим
        _ms_data = getattr(md, "market_structure", None)
        _klines_4h = _ms_data and getattr(_ms_data, "has_4h", False)
        _klines_1d = _ms_data and getattr(_ms_data, "has_1d", False)
        if ohlcv_4h and len(ohlcv_4h) >= 20:
            try:
                _pat_4h = state.pattern_detector.detect_all(ohlcv_4h, None, md)
                for _p in _pat_4h:
                    # Увеличиваем вес HTF паттернов — они надёжнее
                    _p.score_bonus = int(_p.score_bonus * 1.3)
                    _p.name = f"{_p.name}_4H"
                patterns = patterns + _pat_4h
            except Exception:
                pass
        if _ms_data and _ms_data.has_1d:
            # ✅ OPT v19: Используем уже загруженные 1D klines из market_structure (no extra API call)
            try:
                # Пробуем получить из кэша через batch-запрос binance (уже загружены)
                _kl_1d = getattr(state.binance, "_last_1d_klines", {}).get(symbol)
                if not _kl_1d:
                    # Fallback: но 1D klines уже загружены в get_complete_market_data
                    # Просто пропускаем отдельный запрос
                    pass
                if _kl_1d and len(_kl_1d) >= 10:
                    _pat_1d = state.pattern_detector.detect_all(_kl_1d, None, md)
                    for _p in _pat_1d:
                        _p.score_bonus = int(_p.score_bonus * 1.6)
                        _p.name = f"{_p.name}_1D"
                    patterns = patterns + _pat_1d
            except Exception:
                pass
        # Дедупликация: убираем паттерны одного типа если уже есть более HTF версия
        _seen_base = set()
        _dedup = []
        for _p in sorted(patterns, key=lambda x: x.score_bonus, reverse=True):
            _base = _p.name.replace("_4H","").replace("_1D","").replace("_30M","")
            if _base not in _seen_base:
                _seen_base.add(_base)
                _dedup.append(_p)
        patterns = _dedup[:8]  # топ-8 паттернов

        # ── P2: Flag / Pennant Detector ───────────────────────────────────────
        _fp_result = None
        try:
            if ohlcv_4h and len(ohlcv_4h) >= 12:
                from core.flag_pennant_detector import detect_flag_pennant
                from core.pattern_detector import PatternResult as _PR2
                _fp_result = detect_flag_pennant(ohlcv_4h, md.price, "short")
                if _fp_result and _fp_result.has_signal:
                    _fp_bonus = min(_fp_result.score_bonus, 20)
                    patterns.append(_PR2(
                        name=_fp_result.pattern_type,
                        score_bonus=_fp_bonus,
                        direction="short",
                        confidence=0.78 if _fp_result.is_breakout else 0.60,
                        reasons=[_fp_result.description],
                    ))
                    if verbose:
                        print(f"{log_prefix} 🚩 [FLAG/PENNANT] {_fp_result.description}")
        except Exception as _fp_err:
            pass  # не критично

        # ── P1: Order Book Score ──────────────────────────────────────────────
        _ob_score = 0
        try:
            if Config.ENABLE_ORDERBOOK and state.auto_trader and hasattr(state.auto_trader, 'bingx') and state.auto_trader.bingx:
                _ob_data = await state.auto_trader.bingx.get_order_book(symbol)
                if _ob_data:
                    from core.orderbook_scorer import calculate_orderbook_score
                    _ob_score, _ob_desc, _ = calculate_orderbook_score(_ob_data, md.price, "short")
                    if verbose and _ob_desc:
                        print(f"{log_prefix} {_ob_desc}")
                else:
                    # Fix #1: BingX 109418 (offline) → постоянный блэклист
                    #         BingX 109425 (not exist) → skip 7 дней
                    _bingx_err = getattr(state.auto_trader.bingx, 'last_error_code', None)
                    if _bingx_err in (109418, 109425):
                        try:
                            if state.redis:
                                if _bingx_err == 109418:
                                    state.redis.client.set(f"blacklist:{symbol}", f"bingx:offline:{_bingx_err}")
                                    if hasattr(state, '_blacklist_set') and state._blacklist_set is not None:
                                        state._blacklist_set.add(symbol)
                                    print(f"🚫 [{symbol}] BingX 109418 offline → постоянный блэклист")
                                else:
                                    state.redis.client.setex(f"skip:nodata:{symbol}", 86400 * 7, f"bingx:notexist:{_bingx_err}")
                                    if hasattr(state, '_skip_nodata_set') and state._skip_nodata_set is not None:
                                        state._skip_nodata_set.add(symbol)
                                    print(f"⏭ [{symbol}] BingX 109425 not exist → skip 7д")
                        except Exception:
                            pass
        except Exception as _ob_err:
            pass  # не критично

        # ── P3: OnChain CoinGecko Volume Z-Score ─────────────────────────────
        _onchain_bonus = 0
        _onchain_desc = ""
        try:
            if os.getenv("ENABLE_ONCHAIN", "true").lower() == "true":
                from core.onchain_client import get_volume_z_score, onchain_score_bonus
                _redis_cli = state.redis.client if state.redis else None
                _z, _zdesc = await asyncio.wait_for(
                    get_volume_z_score(symbol, _redis_cli), timeout=8.0
                )
                _onchain_bonus, _onchain_desc = onchain_score_bonus(_z, "short")
                if verbose and _zdesc:
                    print(f"{log_prefix} {_zdesc}")
        except Exception:
            pass  # не критично

        # ── #35: Active Addresses proxy (7-day volume delta) ─────────────────
        _addr_bonus = 0
        _addr_desc = ""
        try:
            if os.getenv("ENABLE_ONCHAIN", "true").lower() == "true":
                from core.onchain_client import get_active_addr_proxy, addr_proxy_score_bonus
                _redis_cli = state.redis.client if state.redis else None
                _addr_pct, _addr_raw_desc = await asyncio.wait_for(
                    get_active_addr_proxy(symbol, _redis_cli), timeout=8.0
                )
                _addr_bonus, _addr_desc = addr_proxy_score_bonus(_addr_pct, "short")
                if verbose and _addr_desc:
                    print(f"{log_prefix} 📊 [ADDR] {_addr_desc}")
        except Exception:
            pass  # не критично

        # MS-данные доступны вне блока verbose для сохранения в signal
        _ms_log = getattr(md, "market_structure", None)

        # ── PRE-SCORE LOG: все данные перед скорингом ─────────────────
        if verbose:
            top_trader_val = getattr(md, "top_trader_long_short_ratio", None)
            taker_val      = getattr(md, "taker_buy_sell_ratio",       None)
            print(
                f"{log_prefix} 📋 [PRE-SCORE DATA] "
                f"rsi={md.rsi_1h:.1f} | funding={md.funding_rate:.4f}% | "
                f"acc_funding={md.funding_accumulated:.4f}% | "
                f"L/S={md.long_short_ratio:.1f}% | "
                f"OI_15m={getattr(md,'oi_change_15m',0.0):+.1f}% | OI_30m={getattr(md,'oi_change_30m',0.0):+.1f}% | OI_1h={getattr(md,'oi_change_1h',0.0):+.1f}% | OI_4h={getattr(md,'oi_change_4h',0.0):+.1f}% | "
                f"vol_spike={getattr(md,'volume_spike_ratio',1.0):.2f}x | "
                f"atr={getattr(md,'atr_14_pct',0.5):.2f}% | "
                f"top_trader={'%.2f' % top_trader_val if top_trader_val is not None else '⚠️ None'} | "
                f"taker={'%.2f' % taker_val if taker_val is not None else '⚠️ None'} | "
                f"price_trend={price_trend} | "
                f"patterns={[p.name for p in patterns]} | "
                f"hourly_deltas({len(hourly_deltas)})={[round(d,0) for d in hourly_deltas[-5:]]}"
            )
            # MS summary
            if _ms_log:
                from utils.market_structure import format_ms_summary
                print(f"{log_prefix} 🏗 [MS STRUCTURE] {format_ms_summary(_ms_log)}")
        # ─────────────────────────────────────────────────────────────

        # 🆕 OKX Liquidations fallback — ПЕРЕД скорером, чтобы данные попали в md
        if md.recent_liquidations_usd is None or md.liq_side is None:
            try:
                from api.okx_client import get_okx_client
                okx_liq = await get_okx_client().get_liquidations(symbol)
                if okx_liq:
                    md.recent_liquidations_usd = okx_liq["total_usd"]
                    md.liq_side = okx_liq["dominant_side"]
                    if verbose:
                        print(f"{log_prefix} 🔄 [OKX_LIQ] fallback: {okx_liq['dominant_side']} ${okx_liq['total_usd']:.0f}")
            except Exception:
                pass

        # Multi-TF RSI и OI для scorer
        _rsi_15m = _calc_rsi(ohlcv_15m) if ohlcv_15m else None
        _oi_15m  = getattr(md, 'oi_change_15m', 0.0) or 0.0
        _oi_30m  = getattr(md, 'oi_change_30m', 0.0) or 0.0
        _oi_1h   = getattr(md, 'oi_change_1h', 0.0) or 0.0
        _oi_4h   = getattr(md, 'oi_change_4h', 0.0) or 0.0
        # HTF structure и zone из market_structure
        _ms_s    = getattr(md, 'market_structure', None)
        _htf_str = getattr(_ms_s, 'htf_structure', '') or ''
        _zone    = getattr(_ms_s, 'zone_4h', '') or ''
        # 30M delta — вычисляем из уже загруженных 30m свечей (без доп. API вызова)
        _delta_30m = []
        if ohlcv_30m:
            for _c in ohlcv_30m[-14:]:
                _pdp = (_c.close - _c.open) / _c.open if _c.open > 0 else 0
                _delta_30m.append(_c.quote_volume * (1 if _pdp >= 0 else -1))

        base_result = state.scorer.calculate_score(
            rsi_1h=md.rsi_1h or 50,
            funding_current=md.funding_rate,
            funding_accumulated=md.funding_accumulated,
            long_ratio=md.long_short_ratio,
            price_change_24h=md.price_change_24h,
            hourly_deltas=hourly_deltas,
            price_trend=price_trend,
            patterns=patterns,
            volume_spike_ratio=getattr(md, "volume_spike_ratio", 1.0),
            atr_14_pct=getattr(md, "atr_14_pct", 0.5),
            top_trader_ratio=getattr(md, "top_trader_long_short_ratio", None),
            taker_ratio=getattr(md, "taker_buy_sell_ratio", None),
            oi_15m=_oi_15m,
            oi_30m=_oi_30m,
            oi_1h=_oi_1h,
            oi_4h=_oi_4h,
            rsi_15m=_rsi_15m,
            rsi_30m=rsi_30m,
            rsi_4h=rsi_4h,
            htf_structure=_htf_str,
            zone=_zone,
            delta_30m=_delta_30m,
            orderbook_score=_ob_score,
        )

        # Fear & Greed макро-модификатор — применяем ДО проверки is_valid
        fg = state.fear_greed_index
        fg_modifier = 0
        fg_reason = ""
        if fg is not None:
            if fg > 80:
                fg_modifier, fg_reason = 6,  f"🧠 [F&G] {fg} Жадность → SHORT +6"
            elif fg > 65:
                fg_modifier, fg_reason = 3,  f"🧠 [F&G] {fg} Умеренная жадность → SHORT +3"
            elif fg < 20:
                fg_modifier, fg_reason = -2, f"🧠 [F&G] {fg} Экстремальный страх → SHORT -2"
            elif fg < 35:
                fg_modifier, fg_reason = -1, f"🧠 [F&G] {fg} Страх → SHORT -1"

        raw_score      = base_result.total_score
        # ✅ FIX P1: CASCADE учитывается ДО gate-проверки (аналог LONG бота)
        _cas_pre = getattr(md, "cascade_signal", None)
        _cas_pre_bonus = 0
        if _cas_pre is not None and _cas_pre.has_signal and _cas_pre.direction == "short":
            if _cas_pre.score_bonus >= 14:   _cas_pre_bonus = 10
            elif _cas_pre.score_bonus >= 10: _cas_pre_bonus = 7
            elif _cas_pre.score_bonus >= 8:  _cas_pre_bonus = 5
            elif _cas_pre.score_bonus >= 6:  _cas_pre_bonus = 3
        effective_score = max(min(raw_score + fg_modifier + _mtf_bonus + _cas_pre_bonus + _onchain_bonus + _addr_bonus, 100), 0)
        min_score       = state.scorer.min_score

        if verbose:
            fg_str   = f" F&G={fg_modifier:+d}" if fg_modifier != 0 else ""
            mtf_str  = f" MTF={_mtf_bonus:+d}"  if _mtf_bonus  != 0 else ""
            cas_str  = f" CASCADE={_cas_pre_bonus:+d}" if _cas_pre_bonus > 0 else ""
            oc_str   = f" ONCHAIN={_onchain_bonus:+d}" if _onchain_bonus != 0 else ""
            addr_str = f" ADDR={_addr_bonus:+d}"       if _addr_bonus    != 0 else ""
            print(f"{log_prefix} 📊 [BASE_SCORER] score={raw_score}{fg_str}{mtf_str}{cas_str}{oc_str}{addr_str} → {effective_score} (min={min_score})"
                  f" | components: {[(c.name, c.score) for c in base_result.components]}")
            if _cas_pre_bonus > 0:
                print(f"{log_prefix} 🎯 [CASCADE PRE-GATE] +{_cas_pre_bonus}pts → помогает пройти gate")
            if _onchain_desc:
                print(f"{log_prefix} 📊 [ONCHAIN] {_onchain_desc}")
            if base_result.funding_info:
                print(f"{log_prefix} 💰 {base_result.funding_info}")

        if effective_score < min_score:
            if verbose:
                print(f"{log_prefix} ❌ [BASE_SCORER] is_valid=False — базовый скоринг отклонил")
                if fg_reason: print(f"{log_prefix} {fg_reason}")
            return None

        if verbose and fg_reason:
            print(f"{log_prefix} {fg_reason}")

        # ── PARABOLIC MOMENTUM BLOCKER ──────────────────────────────────────
        # Ракету не шортим: OI_4h > 20% + price_24h > 12% + volume_spike > 3x
        _oi4h     = getattr(md, "oi_change_4h", 0.0)
        _vol_sp   = getattr(md, "volume_spike_ratio", 1.0)
        _price24h = abs(md.price_change_24h)
        if _oi4h > 20.0 and _price24h > 12.0 and _vol_sp > 3.0:
            if verbose:
                print(f"{log_prefix} 🚫 [PARABOLIC BLOCK] OI_4h={_oi4h:.1f}% price_24h={_price24h:.1f}% spike={_vol_sp:.1f}x — ракету не шортим")
            return None
        # Более мягкий вариант: price_24h > 20% или OI_4h > 40% в одиночку
        if _oi4h > 40.0 or _price24h > 20.0:
            if verbose:
                print(f"{log_prefix} 🚫 [PARABOLIC BLOCK] Экстремальный импульс OI_4h={_oi4h:.1f}% price24h={_price24h:.1f}% — блок")
            return None

        price      = md.price
        base_score = effective_score
        
        # ── Market Structure Bonus (HTF) ─────────────────────────────────────
        # PDH/PDL, Fib 0.618, OB/FVG 4H, CRT, HTF structure
        _ms = getattr(md, "market_structure", None)
        if _ms is not None:
            try:
                from utils.market_structure import proximity_bonus
                _ms_bonus, _ms_reasons = proximity_bonus(price, _ms, "short")
                if _ms_bonus != 0:
                    base_score = max(0, min(100, base_score + _ms_bonus))
                    if verbose and _ms_reasons:
                        print(f"{log_prefix} 🏗 [MS] {' | '.join(_ms_reasons[:3])}")
            except Exception as _ms_e:
                pass  # MS bonus не критичен

        # ── CASCADE SIGNAL Bonus (4H Fractal Raid → 1H SNR → 15M FVG) ──────
        _cas = getattr(md, "cascade_signal", None)
        if _cas is not None and _cas.has_signal and _cas.direction == "short":
            base_score = max(0, min(100, base_score + _cas.score_bonus))
            if verbose:
                print(f"{log_prefix} 🎯 [CASCADE SHORT] +{_cas.score_bonus}: {_cas.description[:80]}")
        # 🆕 Консолидация фильтр — блокировка входов в середине диапазона
        _cons_filter_on = os.getenv("CONSOLIDATION_FILTER_ENABLED", "true").lower() == "true"
        if _cons_filter_on and state.consolidation_detector and ohlcv_15m:
            cons = state.consolidation_detector.detect(ohlcv_15m, price)
            # ✅ FIX #4: передаём RSI 1H для исключения при экстремальной перегретости
            rsi_1h_val = getattr(md, "rsi_1h", 50.0) or 50.0
            # ✅ FIX #5: HTF bearish → SHORT в lower_half = продолжение тренда, порог RSI снижен до 65
            _htf_is_bearish = "bear" in _htf_str.lower() or "bearish" in _htf_str.lower()
            allow, reason = filter_mid_range(cons, price, "short", verbose=False, rsi_1h=rsi_1h_val, htf_bearish=_htf_is_bearish)
            
            if cons.is_consolidating and not allow:
                # ✅ FIX P4: score≥75 + upthrust/breakout_down → bypass (аналог LONG бота)
                _cons_bypass = (
                    base_score >= 75
                    and (cons.has_upthrust or cons.has_breakout_down)
                )
                if _cons_bypass:
                    if verbose:
                        print(f"{log_prefix} 🟡 [CONSOLIDATION BYPASS] score={base_score:.0f}≥75 + upthrust/breakout → override {reason}")
                else:
                    if verbose:
                        print(f"{log_prefix} ❌ [CONSOLIDATION] {reason}")
                    return None
            
            if cons.has_upthrust and cons.is_consolidating:
                base_score += 12  # Бонус за Upthrust
                if verbose:
                    print(f"{log_prefix} ✅ [UPTHRUST] +12 — ложный пробой вверх")
            
            if cons.has_breakout_down and cons.is_consolidating:
                base_score += 8  # Бонус за пробой вниз
                if verbose:
                    print(f"{log_prefix} ✅ [BREAKOUT] +8 — пробой консолидации")
        
        # ── SHORT-специфичные фильтры (сохраняем) ──
        sf   = get_short_filter()
        filt = sf.check(
            market_data=md, ohlcv_15m=ohlcv_15m,
            hourly_deltas=hourly_deltas,
            btc_price_1h_change=cached_btc_1h,
        )
        if filt.blocked:
            if verbose:
                print(f"{log_prefix} ❌ [SHORT_FILTER] БЛОКИРОВКА: {filt.block_reason} | delta={filt.score_delta:+.1f}")
            return None

        if filt.score_delta != 0 and verbose:
            reason_str = getattr(filt, 'reasons', None)
            reason_str = (", ".join(reason_str) if reason_str else getattr(filt, 'block_reason', 'N/A')) or 'score adjustment'
            print(f"{log_prefix} ⚠️ [SHORT_FILTER] delta={filt.score_delta:+.1f} | причина: {reason_str}")

        base_score += filt.score_delta

        # ── RealtimeScorer (сохраняем) ──
        rt = get_realtime_scorer()
        rt_result = await rt.score(
            direction="short", market_data=md,
            base_score=base_score, hourly_deltas=hourly_deltas,
        )
        if rt_result.early_only:
            if verbose:
                print(f"{log_prefix} ❌ [REALTIME] early_only=True — ранний выход, сигнал слабый")
            return None
        
        if verbose:
            print(f"{log_prefix} 📊 [REALTIME] base={rt_result.base_score:.1f} bonus={rt_result.bonus:+.1f} final={rt_result.final_score:.1f}")
        
        base_score = rt_result.final_score

        # ── PatternML: исторический win-rate бонус/штраф ─────────────────────
        if state.redis and patterns:
            try:
                from core.pattern_ml_scorer import get_pattern_ml_scorer
                _ml = get_pattern_ml_scorer(state.redis, "short")
                _ml_bonus, _ml_reason = _ml.get_bonus([p.name for p in patterns])
                if _ml_bonus != 0:
                    base_score = max(0, min(100, base_score + _ml_bonus))
                    if verbose:
                        print(f"{log_prefix} 🤖 [PatternML] {_ml_reason}")
            except Exception as _ml_e:
                pass  # ML scorer не критичен

        # SL порядок приоритетов: Swing SL → ATR SL → Fixed %
        entry_price = price

        # ── #29 Swing High/Low SL (рыночная структура, приоритет 1) ─────────
        _swing_sl_used = False
        try:
            from core.swing_sl import calculate_swing_sl
            if ohlcv_4h and len(ohlcv_4h) >= 10:
                _sw_sl, _sw_desc = calculate_swing_sl(ohlcv_4h, price, "short")
                if _sw_sl is not None:
                    stop_loss = _sw_sl
                    _swing_sl_used = True
                    if verbose:
                        print(f"{log_prefix} 🎯 [SWING SL] {_sw_desc}")
        except Exception as _sw_e:
            pass

        # ── ATR-dynamic SL (M1) для SHORT: SL ВЫШЕ входа ──────────────
        # ✅ v2.1: ATR-based SL вместо фиксированного % (приоритет 2)
        if not _swing_sl_used and Config.USE_ATR_SL and ohlcv_4h and len(ohlcv_4h) >= 14:
            try:
                _highs  = [c.high  for c in ohlcv_4h[-15:]]
                _lows   = [c.low   for c in ohlcv_4h[-15:]]
                _closes = [c.close for c in ohlcv_4h[-15:]]
                _trs = []
                for _i in range(1, len(_highs)):
                    _tr = max(_highs[_i] - _lows[_i],
                              abs(_highs[_i] - _closes[_i-1]),
                              abs(_lows[_i]  - _closes[_i-1]))
                    _trs.append(_tr)
                _atr = sum(_trs[-14:]) / 14 if len(_trs) >= 14 else sum(_trs) / len(_trs)
                _atr_sl = price + _atr * Config.ATR_SL_MULT   # SHORT: SL выше
                _atr_sl_pct = (_atr_sl - price) / price * 100
                if Config.ATR_SL_MIN <= _atr_sl_pct <= Config.ATR_SL_MAX:
                    stop_loss = _atr_sl
                    if verbose:
                        print(f"{log_prefix} 📐 [ATR SL] ATR={_atr:.6f} × {Config.ATR_SL_MULT} → SL={stop_loss:.6f} ({_atr_sl_pct:.2f}%)")
                else:
                    stop_loss = price * (1 + Config.SL_BUFFER / 100)
            except Exception:
                stop_loss = price * (1 + Config.SL_BUFFER / 100)
        elif not _swing_sl_used:
            # Fallback fixed % только если ни Swing, ни ATR SL не сработали
            stop_loss = price * (1 + Config.SL_BUFFER / 100)

        # SMC refinement
        smc_data = {}
        smc_bonus = 0
        if Config.ENABLE_SMC:
            try:
                from core.smc_ict_detector import get_smc_result
                smc = get_smc_result(_ohlcv(ohlcv_15m), "short",
                                     base_sl_pct=Config.SL_BUFFER, base_entry=price)
                smc_bonus = smc.score_bonus
                if smc.score_bonus > 0:
                    base_score += smc.score_bonus
                    if verbose:
                        print(f"{log_prefix} ✅ [SMC] бонус +{smc.score_bonus:.1f} | has_ob={smc.has_ob}, has_fvg={smc.has_fvg}")
                if smc.refined_sl and smc.refined_sl > price:
                    stop_loss = smc.refined_sl
                    if verbose:
                        print(f"{log_prefix} 🎯 [SMC] SL refined: {stop_loss:.4f}")
                if smc.ob_entry:
                    entry_price = smc.ob_entry
                smc_data = {"has_ob": smc.has_ob, "has_fvg": smc.has_fvg,
                            "bonus": smc.score_bonus}
            except Exception as e:
                if verbose:
                    print(f"{log_prefix} ⚠️ [SMC] error: {e}")

        sl_pct = round((stop_loss - price) / price * 100, 2)
        if sl_pct < Config.SL_BUFFER:
            stop_loss = price * (1 + Config.SL_BUFFER / 100)
            sl_pct    = Config.SL_BUFFER

        # ── #33/#34 Trend Following detector (bonus + counter-trend penalty) ──
        _trend_result = None
        try:
            from core.trend_detector import detect_trend
            _trend_result = detect_trend(
                candles_4h=ohlcv_4h,
                price_change_1h=getattr(md, "price_change_1h", 0.0) or 0.0,
                price_change_4h=getattr(md, "price_change_4h", 0.0) or 0.0,
                price_change_1d=getattr(md, "price_change_24h", 0.0) or 0.0,
                volume_spike_ratio=getattr(md, "volume_spike_ratio", 1.0) or 1.0,
                direction="short",
            )
            if _trend_result and _trend_result.has_trend:
                base_score = max(0, min(100, base_score + _trend_result.score_bonus))
                if verbose:
                    print(f"{log_prefix} {_trend_result.description}")
        except Exception as _tr_e:
            logger.debug(f"[TrendDetector] short: {_tr_e}")
            pass

        # Dynamic TP
        btc_trend = ("down" if (cached_btc_1h or 0) < -0.5 else
                     "up"   if (cached_btc_1h or 0) > 0.5 else "sideways")
        tp_levels, tp_weights = get_short_tp_config(
            funding_rate=md.funding_rate,
            pattern_name=patterns[0].name if patterns else None,
            btc_trend=btc_trend,
            atr_pct=getattr(md, "atr_14_pct", 0.0),  # ✅ v19: адаптивный RR
        )
        take_profits = [
            (round(price * (1 - tp / 100), 8), tp_weights[i] if i < len(tp_weights) else 15)
            for i, tp in enumerate(tp_levels)
        ]

        # ── AEGIS ENGINE: взвешенный финальный score ──
        aegis_signal = None
        aegis_components = {}
        if state.signal_engine and Config.ENABLE_AEGIS_ENGINE:
            try:
                aegis_signal = await state.signal_engine.generate_signal(
                    symbol=symbol,
                    market_data=md,
                    ohlcv_15m=ohlcv_15m,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    sl_pct=sl_pct,
                    take_profits=take_profits,
                    base_score=base_score,
                )
                if aegis_signal:
                    final_score     = aegis_signal.total_score
                    aegis_components = {
                        k: round(v.raw_score, 1)
                        for k, v in aegis_signal.components.items()
                    }
                else:
                    # Aegis отклонил — не генерируем
                    if verbose:
                        print(f"{log_prefix} ❌ [AEGIS] сигнал отклонён движком (base_score={base_score:.1f})")
                    return None
            except Exception as e:
                print(f"AegisEngine error {symbol}: {e}")
                final_score = base_score
        else:
            final_score = base_score

        if final_score < Config.MIN_SCORE:
            if verbose:
                print(f"{log_prefix} ❌ [FINAL_FILTER] score={final_score:.1f} < MIN={Config.MIN_SCORE} — сигнал отклонён")
                print(f"{log_prefix}    components: {aegis_components if aegis_components else 'N/A'}")
            return None
        
        if verbose:
            print(f"{log_prefix} ✅ [AEGIS] score={final_score:.1f} >= {Config.MIN_SCORE} | components: {aegis_components if aegis_components else 'N/A'}")

        # ── Smart DCA Grid ──
        dca_grid_info = {}
        if state.dca_engine and Config.ENABLE_SMART_DCA:
            try:
                atr_val = state.dca_engine.calculate_atr(ohlcv_15m)
                grid    = state.dca_engine.calculate_grid(
                    symbol=symbol,
                    entry_price=entry_price,
                    capital=state.risk_manager.capital,
                    initial_risk_pct=Config.RISK_PER_TRADE,
                    atr=atr_val,
                    sl_price=stop_loss,
                )
                dca_grid_info = {
                    "levels":        [(lvl.price, lvl.size_usd, lvl.distance_pct)
                                      for lvl in grid.levels],
                    "weighted_avg":  grid.weighted_avg,
                    "total_exposure": grid.total_exposure,
                    "atr":           round(atr_val, 8),
                }
            except Exception as e:
                print(f"DCA grid error {symbol}: {e}")

        # ── Risk Manager: позиционирование ──
        risk_result = None
        if state.risk_manager:
            try:
                open_usd = state.risk_manager.capital * 0.2  # Approximation
                risk_result = state.risk_manager.calculate_position_size(
                    win_rate=0.62,
                    avg_win_pct=5.0,
                    avg_loss_pct=Config.SL_BUFFER,
                    signal_score=final_score,
                    sl_pct=sl_pct,
                    current_exposure_usd=open_usd,
                )
                if verbose and risk_result:
                    print(f"{log_prefix} 💰 [RISK] Kelly pos={risk_result.size_usd:.2f}$ | size={risk_result.size_pct:.2f}%")
            except Exception as e:
                if verbose:
                    print(f"{log_prefix} ⚠️ [RISK] error: {e}")

        # ── Performance tracking ──
        if state.performance_tracker:
            strength_str = aegis_signal.strength.value if aegis_signal else "N/A"
            state.performance_tracker.record_signal(symbol, final_score, strength_str)

        # ── Reasons assembly ──
        reasons = list(base_result.reasons)
        reasons.extend(rt_result.factors)
        if aegis_signal:
            reasons.extend(aegis_signal.reasons[:6])

        return {
            "symbol":       symbol,
            "direction":    "short",
            "score":        round(final_score, 1),
            "grade":        aegis_signal.grade if aegis_signal else base_result.grade,
            "strength":     aegis_signal.strength.value if aegis_signal else "N/A",
            "confidence":   base_result.confidence.value,
            "price":        price,
            "entry_price":  entry_price,
            "stop_loss":    round(stop_loss, 8),
            "sl_pct":       sl_pct,
            "take_profits": take_profits,
            "patterns":     [p.name for p in patterns],
            "best_pattern": patterns[0].name if patterns else None,
            "indicators": {
                "RSI":      f"{md.rsi_1h:.1f}" if md.rsi_1h else "N/A",
                "Funding":  f"{md.funding_rate:+.3f}%",
                "L/S":      f"{md.long_short_ratio:.0f}% longs",
                "OI 15m":   f"{getattr(md,'oi_change_15m',0.0):+.1f}%",
                "OI 1h":    f"{getattr(md,'oi_change_1h',0.0):+.1f}%",
                "OI 4h":    f"{getattr(md,'oi_change_4h',0.0):+.1f}%",
                "Price 24h": f"{md.price_change_24h:+.1f}%",
            },
            "aegis_components": aegis_components,
            "dca_grid":     dca_grid_info,
            "risk": {
                "size_usd":    risk_result.size_usd    if risk_result else None,
                "size_pct":    risk_result.size_pct    if risk_result else None,
                "kelly_pct":   risk_result.kelly_pct   if risk_result else None,
                "risk_usd":    risk_result.risk_usd    if risk_result else None,
            },
            "smc":       smc_data,
            "reasons":   reasons[:14],
            # Compatibility fields
            "rsi_1h":           round(md.rsi_1h or 0, 1),
            "funding_rate":     round(md.funding_rate, 4),
            "oi_change":        round(getattr(md, 'oi_change_1h', 0.0), 2),
            "long_short_ratio": round(md.long_short_ratio, 1),
            "volume_spike_ratio": round(getattr(md, "volume_spike_ratio", 1.0), 2),
            "atr_14_pct":       round(getattr(md, "atr_14_pct", 0.5), 3),
            "pattern":          patterns[0].name if patterns else "",
            "timestamp":        datetime.utcnow().isoformat(),
            "status":           "active",
            "taken_tps":        [],
            # MS-данные для дашборда (pivot, PDH/PDL, CME gap)
            "ms_pivot_pp":  round(getattr(_ms_log, "pivot_pp",  0) or 0, 8) if _ms_log else 0,
            "ms_pivot_r1":  round(getattr(_ms_log, "pivot_r1",  0) or 0, 8) if _ms_log else 0,
            "ms_pivot_s1":  round(getattr(_ms_log, "pivot_s1",  0) or 0, 8) if _ms_log else 0,
            "ms_pdh":       round(getattr(_ms_log, "pdh",        0) or 0, 8) if _ms_log else 0,
            "ms_pdl":       round(getattr(_ms_log, "pdl",        0) or 0, 8) if _ms_log else 0,
            "ms_cme_gap_pct":   round(getattr(_ms_log, "cme_gap_pct",  0) or 0, 3) if _ms_log else 0,
            "ms_cme_gap_dir":   getattr(_ms_log, "cme_gap_dir", "none") if _ms_log else "none",
            "ms_cme_gap_low":   round(getattr(_ms_log, "cme_gap_low",  0) or 0, 8) if _ms_log else 0,
            "ms_cme_gap_high":  round(getattr(_ms_log, "cme_gap_high", 0) or 0, 8) if _ms_log else 0,
            "ms_has_cme_gap":   bool(getattr(_ms_log, "has_cme_gap",   False)) if _ms_log else False,
            "ms_zone_4h":       getattr(_ms_log, "zone_4h", "neutral") if _ms_log else "neutral",
            "ms_htf_structure": getattr(_ms_log, "htf_structure", "unknown") if _ms_log else "unknown",
        }
        
        print(f"🟢 [SIGNAL] {symbol}: score={final_score:.1f} grade={signal['grade']} — сигнал создан и отправлен в Telegram!")
        return signal

    except Exception as e:
        print(f"Error scanning {symbol}: {e}")
        return None


async def scan_market():
    if state.is_paused:
        return

    # ✅ FIX: Prevent concurrent/duplicate scans (process-level guard)
    if getattr(state, '_scan_running', False):
        print(f"⏳ [{Config.BOT_TYPE}] Scan already running. Skipping duplicate.")
        return
    state._scan_running = True

    # ✅ FIX: Redis distributed lock (cross-restart protection)
    lock_key = f"scan_lock:{Config.BOT_TYPE}"
    _lock_held = False
    try:
        _lock_held = bool(state.redis.client.set(lock_key, 1, nx=True, ex=300))
        if not _lock_held:
            print(f"⏳ [{Config.BOT_TYPE}] Redis scan lock held. Skipping.")
            state._scan_running = False
            return
    except Exception as e:
        print(f"⚠️ Redis lock error: {e} — proceeding without distributed lock")

    print(f"\n🔍 {Config.BOT_NAME} scan at {datetime.utcnow().strftime('%H:%M:%S UTC')}")
    print(f"📊 {len(state.watchlist)} symbols | Score≥{Config.MIN_SCORE}")

    # Circuit breaker check
    if state.risk_manager:
        blocked, reason = state.risk_manager.check_circuit_breakers()
        if blocked:
            print(f"⛔ Circuit Breaker: {reason}")
            await state.telegram.send_message(
                f"⛔ <b>CIRCUIT BREAKER АКТИВИРОВАН</b>\n\n{reason}\n\n"
                f"Используйте /reset для сброса"
            )
            # ✅ FIX: Release locks on early return
            state._scan_running = False
            if _lock_held:
                try: state.redis.client.delete(lock_key)
                except Exception: pass
            return

    # BTC cache
    _btc_cache_1h: Optional[float] = None
    try:
        _btc_md = await state.binance.get_complete_market_data("BTCUSDT")
        if _btc_md:
            _btc_cache_1h = _btc_md.price_change_1h
    except Exception:
        pass

    # ✅ OPT: Batch-загрузка blacklist + skip:nodata в Python sets (2 Redis команды вместо ~1000)
    # Экономия: 487 символов × 2 EXISTS = 974 команды → 2 keys() вызова на весь скан
    try:
        if state.redis:
            _bl_keys = state.redis.client.keys("blacklist:*")
            state._blacklist_set = {k.replace("blacklist:", "") for k in _bl_keys}
            _sk_keys = state.redis.client.keys("skip:nodata:*")
            state._skip_nodata_set = {k.replace("skip:nodata:", "") for k in _sk_keys}
    except Exception:
        state._blacklist_set = None
        state._skip_nodata_set = None

    # ✅ OPT v18: Batch-загрузка ВСЕХ тикеров за 1 запрос (кэш 60s)
    # Без этого: 341 запрос × /v5/market/tickers → +30s на скан
    try:
        await state.binance._fetch_ticker_batch()
    except Exception as _tb_e:
        pass  # fallback: одиночные запросы в get_complete_market_data

    # BTC correlation score adj
    btc_adj = 0
    if _btc_cache_1h is not None:
        if _btc_cache_1h < -2.0:   btc_adj = +3
        elif _btc_cache_1h < -0.5: btc_adj = +1
        elif _btc_cache_1h > 2.0:  btc_adj = -3
        elif _btc_cache_1h > 0.5:  btc_adj = -1

    # ── ADAPTIVE MIN_SCORE по Fear & Greed + BTC тренду ─────────────────────
    # ✅ FIX: Adaptive base = MIN_SHORT_BASE_SCORE (было Config.MIN_SCORE=55 → игнорировало ENV)
    # ✅ FIX: Убран мёртвый else-код (if "short"=="short" всегда True → else недостижим)
    # ✅ FIX: Ceiling = base + ADAPTIVE_MAX_BOOST (было хардкод 78)
    _base_aegis = int(os.getenv("MIN_SHORT_BASE_SCORE", "58"))  # читаем напрямую
    _adaptive_score = _base_aegis
    _fg = state.fear_greed_index
    if _fg is not None:
        # SHORT: жадность → лучше шортить (снижаем порог), страх → плохо для шорта (повышаем)
        if _fg > 75:   _adaptive_score -= 5
        elif _fg > 65: _adaptive_score -= 2
        elif _fg < 25: _adaptive_score += 5
        elif _fg < 35: _adaptive_score += 2
    if _btc_cache_1h is not None:
        # SHORT: BTC растёт = шортить сложнее (повышаем порог), BTC падает = шорты проще
        if _btc_cache_1h > 3.0:  _adaptive_score += 3
        elif _btc_cache_1h < -2.0: _adaptive_score -= 3
    # Clamp: не выше base+ADAPTIVE_MAX_BOOST, не ниже base-5
    _adaptive_score = max(_base_aegis - 5, min(_base_aegis + Config.ADAPTIVE_MAX_BOOST, _adaptive_score))
    if _adaptive_score != _base_aegis:
        print(f"🎯 [ADAPTIVE] SHORT min: {_base_aegis} → {_adaptive_score} "
              f"(F&G={_fg}, BTC_1h={(_btc_cache_1h or 0):.1f}%)")
    state._adaptive_min_score = _adaptive_score

    active_count  = await _count_real_positions()
    exchange_full = active_count >= Config.MAX_POSITIONS
    if exchange_full:
        print(f"📊 Exchange: {active_count}/{Config.MAX_POSITIONS} SHORT slots — TG-only mode")

    new_signals   = 0
    tg_only_count = 0
    rejected_count = 0  # Счётчик отклонённых сигналов
    
    # Счётчики причин отклонения
    reject_reasons = {
        "no_data": 0,
        "base_scorer": 0,
        "short_filter": 0,
        "realtime": 0,
        "aegis": 0,
        "low_score": 0,
    }

    # ✅ FIX БАГ 5: Параллельный pre-fetch — сокращает скан с ~5 мин до ~30с
    # SCAN_CONCURRENCY=8 означает 8 символов одновременно (по умолчанию)
    _SCAN_SEM = asyncio.Semaphore(int(os.getenv("SCAN_CONCURRENCY", "12")))  # ✅ FIX v17: 8→12
    _FRESH = object()  # sentinel: символ свежий, пропускаем

    async def _prefetch(sym: str):
        async with _SCAN_SEM:
            if hasattr(state, "_adaptive_min_score"): state.scorer.min_score = state._adaptive_min_score
            try:
                if _is_fresh(state.redis.get_signals(Config.BOT_TYPE, sym, limit=1)):
                    return sym, _FRESH
                sig = await scan_symbol(sym, _btc_cache_1h)
                return sym, sig
            except Exception as _pfe:
                print(f"⚠️ Prefetch {sym}: {_pfe}")
                return sym, None

    _t0 = datetime.utcnow()
    _prefetch_tasks = [_prefetch(s) for s in state.watchlist]
    _prefetch_results = await asyncio.gather(*_prefetch_tasks)
    _dt = (datetime.utcnow() - _t0).total_seconds()
    print(f"⚡ Parallel fetch: {len(state.watchlist)} symbols in {_dt:.1f}s")
    _prefetch_map = dict(_prefetch_results)

    for symbol in state.watchlist:
        try:
            _fetched = _prefetch_map.get(symbol)
            if _fetched is _FRESH:
                continue
            signal = _fetched
            if not signal:
                rejected_count += 1
                continue

            # BTC correlation adj
            signal["score"] = round(signal["score"] + btc_adj, 1)
            # ✅ FIX: Cap score at 100.0 (prevents overflow display like 110.5)
            signal["score"] = min(signal["score"], 100.0)
            if signal["score"] < Config.MIN_SCORE:
                continue

            # ✅ FIX: RR pre-check BEFORE Telegram — don't alert on signals we won't trade
            _MIN_RR = 1.0  # matches AutoTrader TradeConfig.min_rr_ratio
            _tp_list = signal.get("take_profits", [])
            if _tp_list:
                _tp1_raw = _tp_list[0]
                try:
                    if isinstance(_tp1_raw, (list, tuple)):   _tp1 = float(_tp1_raw[0])
                    elif isinstance(_tp1_raw, dict):          _tp1 = float(_tp1_raw.get("price", 0))
                    else:                                      _tp1 = float(_tp1_raw)
                    _sl_dist  = abs(signal["entry_price"] - signal["stop_loss"])
                    _tp1_dist = abs(_tp1 - signal["entry_price"])
                    _rr = _tp1_dist / _sl_dist if _sl_dist > 0 else 0
                    if _rr < _MIN_RR:
                        print(f"⏸ [{signal['symbol']}][SHORT] RR={_rr:.2f} < {_MIN_RR} — pre-filtered before Telegram")
                        rejected_count += 1
                        continue
                except Exception as _rr_err:
                    print(f"⚠️ RR pre-check error {signal['symbol']}: {_rr_err}")

            # Telegram сигнал
            tg_msg_id = await state.telegram.send_signal(
                direction="short", symbol=signal["symbol"],
                score=signal["score"], price=signal["price"],
                pattern=signal.get("strength", signal.get("best_pattern") or "N/A"),
                indicators=signal["indicators"],
                entry=signal["entry_price"],
                stop_loss=signal["stop_loss"],
                take_profits=signal["take_profits"],
                leverage=Config.LEVERAGE, risk="Kelly-sized",
            )
            signal["tg_msg_id"] = tg_msg_id

            # Дополняем сигнал Aegis-информацией (человекочитаемый формат)
            if signal.get("aegis_components"):
                comps = signal["aegis_components"]
                grade   = signal.get("grade", "N/A")
                strength = signal.get("strength", "N/A")
                grade_emoji = {"A+": "💎", "A": "🥇", "B": "🥈", "C": "🥉", "D": "⚠️"}.get(grade, "📊")
                strength_ru = {
                    "ULTRA": "🚀 ЭКСТРЕМАЛЬНЫЙ", "STRONG": "⚡ СИЛЬНЫЙ",
                    "MODERATE": "✅ УМЕРЕННЫЙ", "WATCH": "👀 СЛАБЫЙ", "NOISE": "🔕 ШУМ"
                }.get(str(strength), str(strength))

                def bar(v): return "▓" * int(v/10) + "░" * (10 - int(v/10))

                comp_names = {
                    "z_volume":     ("📊 Объём/Z-скор", "памп или дамп vs VWAP"),
                    "oi_change":    ("📈 OI + L/S",     "открытый интерес и позиции толпы"),
                    "funding_rate": ("💸 Фандинг",      "кто переплачивает за позицию"),
                    "smc_structure":("🏗 Структура",    "CHoCH / OB / FVG по SMC"),
                    "delta_flow":   ("⚡ Дельта",       "агрессивные покупки / продажи"),
                    "rsi_aux":      ("📉 RSI aux",      "RSI как вспомогательный"),
                }
                lines = ""
                for k, v in comps.items():
                    name, desc = comp_names.get(k, (k, ""))
                    score_val = int(v)
                    lines += f"  {name}: <b>{score_val}</b>/100  {bar(score_val)}\n"
                    lines += f"    <i>{desc}</i>\n"

                # Определяем статус ДО попытки открытия
                demo_flag = " [DEMO]" if Config.BINGX_DEMO else " [REAL]"
                if not Config.AUTO_TRADING:
                    auto_status = "📋 Только уведомление"
                elif exchange_full:
                    auto_status = "📊 TG-уведомление (биржа заполнена)"
                elif state.is_paused:
                    auto_status = "⏸ Бот на паузе"
                else:
                    auto_status = f"⏳ Открываем на BingX{demo_flag}..."

                await state.telegram.send_message(
                    f"{grade_emoji} <b>Aegis-анализ SHORT — {signal['symbol']}</b>\n"
                    f"Оценка: <b>{grade}</b> | Сила: {strength_ru}\n\n"
                    f"{lines}\n"
                    f"<i>ℹ️ Это компоненты Aegis-движка — дополнительный фильтр качества после базового скора.\n"
                    f"Чем выше каждый компонент, тем сильнее совпадение с шорт-условиями.</i>\n"
                    f"🔄 Статус: {auto_status}"
                )

            state.redis.save_signal(Config.BOT_TYPE, symbol, signal)

            # Биржевое исполнение
            trade_result = None
            if not exchange_full and Config.AUTO_TRADING and not state.is_paused:
                if state.auto_trader:
                    try:
                        trade_result = await state.auto_trader.execute_signal(signal)
                        if trade_result:
                            active_count += 1
                            exchange_full = active_count >= Config.MAX_POSITIONS
                    except Exception as e:
                        print(f"AutoTrader error {symbol}: {e}")
                new_signals += 1
            else:
                tg_only_count += 1

            # ── Отправляем итоговый статус после попытки открытия ─────────────
            if signal.get("aegis_components"):
                demo_flag = " [DEMO]" if Config.BINGX_DEMO else " [REAL]"
                if exchange_full and not trade_result:
                    final_status = f"📊 Виртуальная (биржа заполнена {active_count}/{Config.MAX_POSITIONS})"
                    if state.redis:
                        saved = state.redis.save_virtual_position(Config.BOT_TYPE, symbol, signal)
                        if saved:
                            print(f"✅ Virtual position saved (exchange full): {symbol}")
                elif trade_result:
                    exchange_label = "BingX DEMO" if Config.BINGX_DEMO else "BingX REAL"
                    final_status = f"✅ Открыта на {exchange_label}"
                elif not Config.AUTO_TRADING or state.is_paused or state.auto_trader is None:
                    final_status = f"✅ Позиция открыта (ВИРТУАЛЬНО) без исполнения на бирже BingX{demo_flag}"
                    if state.redis:
                        saved = state.redis.save_virtual_position(Config.BOT_TYPE, symbol, signal)
                        if saved:
                            print(f"✅ Virtual position saved: {symbol}")
                else:
                    final_status = "⚠️ Виртуальная сделка (ошибка биржи или RR)"
                await state.telegram.send_message(
                    f"🔄 <b>#{symbol}</b> — итог: {final_status}"
                )

            await asyncio.sleep(0.5)

        except Exception as e:
            print(f"Error {symbol}: {e}")

    state.daily_signals += new_signals + tg_only_count
    state.last_scan      = datetime.utcnow()
    state.active_signals = len(state.redis.get_active_signals(Config.BOT_TYPE))

    state.redis.update_bot_state(Config.BOT_TYPE, {
        "status":         "paused" if state.is_paused else "running",
        "last_scan":      state.last_scan.isoformat(),
        "daily_signals":  state.daily_signals,
        "active_signals": state.active_signals,
        "version":        Config.BOT_VERSION,
    })
    print(f"✅ Scan done. Signals: {new_signals} | TG-only: {tg_only_count} | "
          f"Rejected: {rejected_count} | Exchange: {active_count}/{Config.MAX_POSITIONS}")
    print(f"   ℹ️  Reject tracking now via [AEGIS REJECT] / [BASE_SCORER] / [REALTIME] log lines above")

    # ✅ FIX: Release locks
    state._scan_running = False
    if _lock_held:
        try:
            state.redis.client.delete(lock_key)
        except Exception:
            pass


async def background_scanner():
    while state.is_running:
        if not state.is_paused:
            try:
                await scan_market()
            except Exception as e:
                print(f"Scanner error: {e}")
        await asyncio.sleep(Config.SCAN_INTERVAL)


async def _watchlist_refresh_task():
    """✅ C3: Обновляем вотчлист каждые WATCHLIST_REFRESH_H часов."""
    refresh_interval = int(Config.WATCHLIST_REFRESH_H * 3600)
    print(f"[WATCHLIST] Авто-обновление каждые {Config.WATCHLIST_REFRESH_H:.1f}ч")
    while state.is_running:
        await asyncio.sleep(refresh_interval)
        try:
            old_count = len(state.watchlist)
            new_wl = await _build_combined_watchlist(
                state.binance, Config.MIN_VOLUME_USDT, Config.MAX_WATCHLIST
            )
            if new_wl:
                state.watchlist = new_wl
                print(f"[WATCHLIST] ✅ Обновлён: {old_count} → {len(new_wl)} монет")
            else:
                print("[WATCHLIST] ⚠️ Обновление пустое — оставляем старый")
        except Exception as e:
            print(f"[WATCHLIST] ❌ Ошибка: {e}")


async def _startup_sl_sync():
    """
    🚨 FIX: SL=0.000000 bug — синхронизация при старте.

    SHORT бот обрабатывает ТОЛЬКО SHORT позиции.
    LONG позиции — ответственность long-bot.
    Защита от дублирования через Redis-ключ (TTL 10 мин).
    """
    await asyncio.sleep(10)  # Ждём инициализации всех компонентов

    if not state.auto_trader or not state.auto_trader.bingx:
        print("[SL-SYNC] AutoTrader не инициализирован — пропускаем")
        return

    print("[SL-SYNC] 🔍 Проверка SL на SHORT BingX позициях...")
    try:
        positions = await state.auto_trader.bingx.get_positions()
        if not positions:
            print("[SL-SYNC] Нет открытых позиций")
            return

        fixed = 0
        skipped = 0
        for p in positions:
            # ✅ FIX: SHORT бот трогает ТОЛЬКО SHORT позиции
            if p.position_side != "SHORT":
                continue

            sym = p.symbol  # уже в формате "BTC-USDT"
            direction = "short"
            entry = p.entry_price

            # ✅ Защита от дублирования: проверяем Redis-ключ
            _dedup_key = f"sl_sync_done:short:{sym}"
            try:
                if state.redis._client.exists(_dedup_key):
                    print(f"[SL-SYNC] {sym} SHORT: уже синкован недавно — пропускаем")
                    continue
            except Exception:
                pass

            if p.stop_loss and p.stop_loss > 0:
                # SL есть на бирже — только синкуем Redis если пустой
                _redis_sig = state.redis.get_position(Config.BOT_TYPE, sym.replace("-", ""))
                if _redis_sig and _f(_redis_sig.get("stop_loss", 0)) <= 0:
                    _redis_sig["stop_loss"] = p.stop_loss
                    state.redis.save_position(Config.BOT_TYPE, sym.replace("-", ""), _redis_sig)
                    print(f"[SL-SYNC] {sym} SHORT: Redis SL пустой, записан {p.stop_loss:.6f} с биржи")
                continue

            # SL отсутствует на бирже — рассчитываем аварийный
            if entry <= 0:
                skipped += 1
                continue

            sl_pct = Config.SL_BUFFER
            sl = entry * (1 + sl_pct / 100)  # SHORT: SL ВЫШЕ entry

            pos_side = "SHORT"
            ok = await state.auto_trader.bingx.update_stop_loss(sym, pos_side, sl, direction)

            if ok:
                fixed += 1
                print(f"[SL-SYNC] ✅ {sym} SHORT: аварийный SL={sl:.6f} выставлен")
                # Обновляем Redis
                _redis_sig = state.redis.get_position(Config.BOT_TYPE, sym.replace("-", ""))
                if _redis_sig:
                    _redis_sig["stop_loss"] = sl
                    state.redis.save_position(Config.BOT_TYPE, sym.replace("-", ""), _redis_sig)
                # Деdup-ключ: не дублировать в течение 10 мин
                try:
                    state.redis._client.setex(_dedup_key, 600, "1")
                except Exception:
                    pass
                # Уведомляем в TG
                await state.telegram.send_message(
                    f"🚨 <b>SL SYNC</b> — аварийный стоп выставлен\n\n"
                    f"🔴 <code>#{sym}</code> SHORT\n"
                    f"📍 Вход: <b>{entry:.6f}</b>\n"
                    f"🛑 Новый SL: <b>{sl:.6f}</b> ({sl_pct}%)\n"
                    f"<i>⚠️ Позиция не имела SL — исправлено при старте</i>"
                )
            else:
                skipped += 1
                print(f"[SL-SYNC] ❌ {sym}: не удалось выставить SL")

        print(f"[SL-SYNC] Итого: {fixed} исправлено, {skipped} пропущено из {len(positions)}")

    except Exception as e:
        import traceback
        print(f"[SL-SYNC] ❌ Ошибка: {e}\n{traceback.format_exc()}")


def _f(v) -> float:
    try:   return float(v)
    except: return 0.0


async def _fear_greed_task():
    """Fear & Greed Index polling — обновляем каждые 30 мин (alternative.me, без ключа)."""
    from core.fear_greed import get_fear_greed
    fg_cache = get_fear_greed()
    while state.is_running:
        try:
            value = await fg_cache.get()
            if value is not None:
                state.fear_greed_index = value
                print(f"🧠 Fear & Greed: {value} ({fg_cache.label})")
        except Exception as e:
            pass  # некритично — продолжаем без F&G
        await asyncio.sleep(1800)  # 30 минут


async def _daily_report_task():
    """Ежедневный отчёт в 09:00 UTC"""
    while state.is_running:
        now = datetime.utcnow()
        # Следующий 09:00 UTC
        next_report = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now >= next_report:
            next_report += timedelta(days=1)
        await asyncio.sleep((next_report - now).total_seconds())

        if state.is_running and state.telegram:
            try:
                # Используем Redis-историю (те же данные что и /daily_rep)
                # PerformanceTracker хранит данные только в RAM и обнуляется при рестарте
                # ✅ FIX: cmd_daily_report на TelegramCommandHandler, не TelegramBot
                if hasattr(state.telegram, "cmd_daily_report"):
                    await state.telegram.cmd_daily_report("", state.telegram.chat_id)
                elif state.telegram_handler and hasattr(state.telegram_handler, "cmd_daily_report"):
                    await state.telegram_handler.cmd_daily_report("", state.telegram.chat_id)
                else:
                    await state.telegram._send_daily_report() if hasattr(state.telegram, "_send_daily_report") else None
                print("✅ Daily report sent (Redis-based)")
            except Exception as e:
                print(f"Daily report error: {e}")


# ============================================================================
# ROUTES
# ============================================================================

@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return JSONResponse({
        "status": "ok", "bot": Config.BOT_NAME, "version": Config.BOT_VERSION,
        "watchlist": len(state.watchlist), "active": state.active_signals,
        "aegis_engine": state.signal_engine is not None,
    })

@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return JSONResponse({
        "bot": Config.BOT_NAME, "version": Config.BOT_VERSION,
        "status": "running" if state.is_running else "stopped",
    })

@app.get("/status")
async def status():
    cb_status = {}
    if state.risk_manager:
        cb_status = state.risk_manager.get_portfolio_heat(0)
    return {
        "bot":         Config.BOT_NAME,
        "version":     Config.BOT_VERSION,
        "is_running":  state.is_running,
        "is_paused":   state.is_paused,
        "watchlist_count": len(state.watchlist),
        "active_signals":  state.active_signals,
        "last_scan":       state.last_scan.isoformat() if state.last_scan else None,
        "risk_manager":    cb_status,
        "config": {
            "min_score":    Config.MIN_SCORE,
            "sl_buffer":    Config.SL_BUFFER,
            "scan_interval": Config.SCAN_INTERVAL,
            "max_pairs":    Config.MAX_PAIRS,
            "dca_levels":   Config.DCA_LEVELS,
            "auto_trading": Config.AUTO_TRADING,
            "aegis_engine": Config.ENABLE_AEGIS_ENGINE,
        },
    }

@app.post("/api/scan")
async def trigger_scan(background_tasks: BackgroundTasks):
    if not state.is_running:
        raise HTTPException(503, "Bot not running")
    if state.is_paused:
        raise HTTPException(503, "Bot is paused")
    # ✅ FIX: не запускаем если скан уже идёт (процессный флаг)
    if getattr(state, '_scan_running', False):
        return {"message": "Scan already running", "skipped": True}
    background_tasks.add_task(scan_market)
    return {"message": "Scan triggered", "timestamp": datetime.utcnow().isoformat()}

@app.get("/api/blacklist")
async def get_blacklist():
    """Просмотр постоянного блэклиста символов без данных."""
    try:
        keys = state.redis.client.keys("blacklist:*")
        bl = {k.replace("blacklist:", ""): state.redis.client.get(k) for k in keys}
        skip_keys = state.redis.client.keys("skip:nodata:*")
        skip = [k.replace("skip:nodata:", "") for k in skip_keys]
        return {"permanent_blacklist": bl, "count": len(bl),
                "temp_skip_24h": skip, "temp_count": len(skip),
                "env_blacklist": list(Config.SYMBOL_BLACKLIST)}
    except Exception as e:
        return {"error": str(e)}

@app.delete("/api/blacklist/{symbol}")
async def remove_from_blacklist(symbol: str):
    """Убрать символ из постоянного блэклиста (если данные появились)."""
    symbol = symbol.upper()
    try:
        state.redis.client.delete(f"blacklist:{symbol}")
        state.redis.client.delete(f"skip:nodata:{symbol}")
        state.redis.client.delete(f"nodata_count:{symbol}")
        return {"message": f"{symbol} удалён из блэклиста"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/signals")
async def get_signals():
    signals = state.redis.get_active_signals(Config.BOT_TYPE)
    return {"count": len(signals), "signals": signals}

@app.get("/api/performance")
async def get_performance():
    if state.performance_tracker:
        return state.performance_tracker.get_stats(7)
    return {"error": "PerformanceTracker not initialized"}

@app.get("/api/risk")
async def get_risk():
    if state.risk_manager:
        return {
            "status": state.risk_manager.status_report(),
            "stats":  state.risk_manager.get_win_stats(),
            "heat":   state.risk_manager.get_portfolio_heat(0),
        }
    return {"error": "RiskManager not initialized"}

@app.get("/api/dca/{symbol}")
async def get_dca_grid(symbol: str):
    """Показывает DCA сетку для символа"""
    if not state.dca_engine:
        return {"error": "DCA engine disabled"}
    try:
        grid = state.dca_engine.calculate_grid(
            symbol=symbol.upper() + "USDT" if not symbol.upper().endswith("USDT") else symbol.upper(),
            entry_price=1.0,   # Placeholder (нужна реальная цена)
            capital=state.risk_manager.capital if state.risk_manager else 1000,
            initial_risk_pct=Config.RISK_PER_TRADE,
        )
        return {
            "symbol":         grid.symbol,
            "entry_price":    grid.entry_price,
            "levels":         [(l.price, l.size_usd, l.distance_pct) for l in grid.levels],
            "total_exposure": grid.total_exposure,
            "weighted_avg":   grid.weighted_avg,
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/positions")
async def get_positions():
    if state.auto_trader:
        pos = await state.auto_trader.bingx.get_positions()
        return {"count": len(pos), "positions": [
            {"symbol": p.symbol, "side": p.side, "size": p.size,
             "entry": p.entry_price, "upnl": p.unrealized_pnl}
            for p in pos
        ]}
    return {"count": 0, "positions": []}

@app.post("/api/circuit-breaker/reset")
async def reset_cb():
    if state.risk_manager:
        state.risk_manager.reset_circuit_breaker(force=True)
        return {"message": "Circuit breaker reset"}
    return {"error": "RiskManager not initialized"}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
        if state.cmd_handler:
            await state.cmd_handler.handle_update(update)
        return {"ok": True}
    except Exception as e:
        print(f"Webhook error: {e}")
        return {"ok": False}

@app.get("/webhook/info")
async def webhook_info():
    if state.telegram:
        return {"webhook": await state.telegram.get_webhook_info()}
    return {"error": "Not initialized"}

@app.get("/webhook/setup")
@app.get("/webhook/reset")
async def setup_webhook():
    render_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not render_url:
        return {"error": "RENDER_EXTERNAL_URL not set"}
    wh_url = f"{render_url}/webhook"
    await state.telegram.delete_webhook()
    await asyncio.sleep(1)
    ok = await state.telegram.setup_webhook(wh_url)
    return {"ok": ok, "url": wh_url}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0",
                port=int(os.getenv("PORT", 8000)), reload=False)
