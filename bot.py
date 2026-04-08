#!/usr/bin/env python3
"""
Polymarket Latency Arbitrage Bot
Default: paper trading mode.
Live trading requires ALL three flags: --live --confirm --i-understand-risks
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import signal
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional, Tuple

import aiohttp
import websockets
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
load_dotenv()

REQUIRED_ENV_VARS = [
    "POLY_API_KEY",
    "POLY_PRIVATE_KEY",
    "TELEGRAM_TOKEN",
    "TELEGRAM_CHAT_ID",
    "ALCHEMY_RPC_URL",
]


def validate_env() -> Dict[str, str]:
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        raise EnvironmentError(f"Missing required env vars: {', '.join(missing)}")
    return {v: os.environ[v] for v in REQUIRED_ENV_VARS}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BINANCE_WS_URI = (
    "wss://stream.binance.com:9443/stream"
    "?streams=btcusdt@ticker/ethusdt@ticker"
)
POLYMARKET_CLOB_URL  = "https://clob.polymarket.com"
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"

STALE_PRICE_SECONDS   = 10
MIN_DETECTABLE_EDGE   = 0.05   # 5 %
MIN_EXECUTION_EDGE    = 0.08   # 8 %
MAX_MARKETS           = 20
MIN_LIQUIDITY_USD     = 50_000

DAILY_HALT_THRESHOLD    = -0.20   # −20 % of day-start balance
DRAWDOWN_HALT_THRESHOLD =  0.60   # portfolio / ATH <= 60 %
CONSEC_LOSS_PAUSE_N     = 5
CONSEC_LOSS_PAUSE_SECS  = 1_800  # 30 min

MAX_POSITION_PCT = 0.08   # hard cap per trade
KELLY_HALF       = 0.5    # half-Kelly multiplier
EXECUTION_TARGET_MS = 800

# Annualised vol used for the log-normal probability model
ASSET_VOL: Dict[str, float] = {"BTC": 0.80, "ETH": 1.00}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging() -> logging.Logger:
    logger = logging.getLogger("polybot")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = RotatingFileHandler(
        "polybot.log", maxBytes=50 * 1024 * 1024, backupCount=5
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


log = setup_logging()


# ---------------------------------------------------------------------------
# Database  (SQLite – paper trades + persistent state)
# ---------------------------------------------------------------------------
class Database:
    def __init__(self, path: str = "paper_trades.db"):
        self.path = path
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> None:
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                market_id   TEXT    NOT NULL,
                asset       TEXT    NOT NULL,
                side        TEXT    NOT NULL,
                size_usdc   REAL    NOT NULL,
                entry_price REAL    NOT NULL,
                exit_price  REAL,
                pnl         REAL,
                edge_pct    REAL    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'open'
            );
            CREATE TABLE IF NOT EXISTS kv_state (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        self._conn.commit()

    # --- paper trade helpers ---

    def trade_open(
        self, market_id: str, asset: str, side: str,
        size_usdc: float, entry_price: float, edge_pct: float,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO paper_trades
               (timestamp, market_id, asset, side, size_usdc, entry_price, edge_pct)
               VALUES (?,?,?,?,?,?,?)""",
            (datetime.utcnow().isoformat(), market_id, asset,
             side, size_usdc, entry_price, edge_pct),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def trade_close(self, trade_id: int, exit_price: float, pnl: float) -> None:
        self._conn.execute(
            "UPDATE paper_trades SET exit_price=?,pnl=?,status='closed' WHERE id=?",
            (exit_price, pnl, trade_id),
        )
        self._conn.commit()

    def today_closed_trades(self) -> List[sqlite3.Row]:
        prefix = date.today().isoformat()
        cur = self._conn.execute(
            "SELECT * FROM paper_trades WHERE timestamp LIKE ? AND status='closed'",
            (f"{prefix}%",),
        )
        return cur.fetchall()

    # --- key-value state helpers ---

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        cur = self._conn.execute(
            "SELECT value FROM kv_state WHERE key=?", (key,)
        )
        row = cur.fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO kv_state (key, value) VALUES (?,?)",
            (key, value),
        )
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
class Telegram:
    def __init__(self, token: str, chat_id: str) -> None:
        self._url     = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()

    async def stop(self) -> None:
        if self._session:
            await self._session.close()

    async def send(self, text: str) -> None:
        if not self._session:
            log.warning("Telegram not started; dropping message")
            return
        try:
            async with self._session.post(
                self._url,
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                },
                timeout=aiohttp.ClientTimeout(total=6),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.error("Telegram %d: %s", resp.status, body[:200])
        except Exception as exc:
            log.error("Telegram send error: %s", exc)


# ---------------------------------------------------------------------------
# Binance price feed
# ---------------------------------------------------------------------------
@dataclass
class PriceSnap:
    price: float
    ts: float  # epoch seconds


class BinanceFeed:
    """
    Streams BTC/USDT and ETH/USDT tickers from Binance combined stream.
    Auto-reconnects with exponential back-off (max 10 retries).
    Marks prices as stale after STALE_PRICE_SECONDS seconds.
    """

    def __init__(self) -> None:
        self._snaps: Dict[str, PriceSnap] = {}
        self._lock    = asyncio.Lock()
        self._running = False

    async def price(self, asset: str) -> Optional[float]:
        """Return fresh price or None if stale / absent."""
        async with self._lock:
            snap = self._snaps.get(asset.upper())
        if snap is None:
            return None
        age = time.time() - snap.ts
        if age > STALE_PRICE_SECONDS:
            log.warning("Stale %s price: %.1f s old", asset, age)
            return None
        return snap.price

    async def is_stale(self, asset: str) -> bool:
        return (await self.price(asset)) is None

    async def _handle(self, raw: str) -> None:
        try:
            msg    = json.loads(raw)
            stream = msg.get("stream", "")          # e.g. "btcusdt@ticker"
            data   = msg.get("data", {})
            asset  = stream.split("@")[0].upper().replace("USDT", "")
            px     = float(data.get("c", 0))
            if px > 0 and asset:
                async with self._lock:
                    self._snaps[asset] = PriceSnap(price=px, ts=time.time())
                log.debug("Price %s = %.2f", asset, px)
        except Exception as exc:
            log.error("BinanceFeed parse: %s", exc)

    async def run(self) -> None:
        self._running = True
        retries = 0
        max_retries = 10

        while self._running:
            try:
                log.info("Connecting to Binance WebSocket…")
                async with websockets.connect(
                    BINANCE_WS_URI,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    retries = 0
                    log.info("Binance WebSocket connected")
                    async for raw in ws:
                        if not self._running:
                            return
                        await self._handle(raw)
            except Exception as exc:
                if not self._running:
                    return
                retries += 1
                if retries > max_retries:
                    log.critical("Binance WS: max retries exceeded — feed stopped")
                    self._running = False
                    return
                delay = min(2 ** retries, 300)
                log.warning(
                    "Binance WS error (%s) — retry %d/%d in %d s",
                    exc, retries, max_retries, delay,
                )
                await asyncio.sleep(delay)

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# Polymarket data structures
# ---------------------------------------------------------------------------
@dataclass
class Market:
    condition_id: str
    token_id:     str     # YES-side CLOB token
    question:     str
    asset:        str     # "BTC" or "ETH"
    strike:       float   # USD strike price extracted from question
    expiry:       datetime
    liquidity:    float   # USD


@dataclass
class OrderBook:
    bids:     List[Tuple[float, float]]
    asks:     List[Tuple[float, float]]
    best_bid: float
    best_ask: float
    mid:      float


# ---------------------------------------------------------------------------
# Polymarket CLOB client  (async wrapper, live orders via thread executor)
# ---------------------------------------------------------------------------
class PolyClient:
    def __init__(
        self,
        api_key: str,
        private_key: str,
        rpc_url: str,
        paper: bool,
    ) -> None:
        self._api_key     = api_key
        self._private_key = private_key
        self._rpc_url     = rpc_url
        self._paper       = paper
        self._session: Optional[aiohttp.ClientSession] = None
        self._executor    = ThreadPoolExecutor(max_workers=4, thread_name_prefix="poly")

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

    async def stop(self) -> None:
        if self._session:
            await self._session.close()
        self._executor.shutdown(wait=False)

    # --- balance ---

    async def balance(self) -> float:
        try:
            async with self._session.get(  # type: ignore[union-attr]
                f"{POLYMARKET_CLOB_URL}/balance",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get("balance", 0))
        except Exception as exc:
            log.error("balance() error: %s", exc)
        return 0.0

    # --- market discovery ---

    async def get_markets(self) -> List[Market]:
        markets: List[Market] = []
        try:
            async with self._session.get(  # type: ignore[union-attr]
                f"{POLYMARKET_GAMMA_URL}/markets",
                params={"active": "true", "closed": "false", "limit": 300},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status != 200:
                    log.warning("get_markets HTTP %d", resp.status)
                    return []
                raw = await resp.json()

            now = datetime.now(timezone.utc)
            items = raw if isinstance(raw, list) else raw.get("markets", [])

            for m in items:
                question = m.get("question", "")
                asset = _extract_asset(question)
                if not asset:
                    continue

                end_str = m.get("endDateIso") or m.get("end_date_iso", "")
                if not end_str:
                    continue
                try:
                    expiry = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                except ValueError:
                    continue

                mins_left = (expiry - now).total_seconds() / 60
                # Only 5-minute and 15-minute windows (1–15 min to expiry)
                if not (1.0 <= mins_left <= 15.0):
                    continue

                liq = float(
                    m.get("liquidityNum") or m.get("volume") or m.get("liquidity") or 0
                )
                if liq < MIN_LIQUIDITY_USD:
                    continue

                strike = _extract_strike(question)
                if strike is None:
                    continue

                tokens = m.get("tokens", [])
                yes_tok = next(
                    (t for t in tokens if str(t.get("outcome", "")).upper() == "YES"),
                    None,
                )
                if not yes_tok:
                    continue

                markets.append(
                    Market(
                        condition_id=m.get("conditionId") or m.get("condition_id", ""),
                        token_id=yes_tok.get("token_id", ""),
                        question=question,
                        asset=asset,
                        strike=strike,
                        expiry=expiry,
                        liquidity=liq,
                    )
                )
                if len(markets) >= MAX_MARKETS:
                    break

        except Exception as exc:
            log.error("get_markets error: %s", exc)

        return markets

    # --- order book ---

    async def orderbook(self, token_id: str) -> Optional[OrderBook]:
        try:
            async with self._session.get(  # type: ignore[union-attr]
                f"{POLYMARKET_CLOB_URL}/book",
                params={"token_id": token_id},
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            bids = [(float(b["price"]), float(b["size"])) for b in data.get("bids", [])]
            asks = [(float(a["price"]), float(a["size"])) for a in data.get("asks", [])]
            if not bids or not asks:
                return None

            best_bid = max(p for p, _ in bids)
            best_ask = min(p for p, _ in asks)
            return OrderBook(
                bids=bids, asks=asks,
                best_bid=best_bid, best_ask=best_ask,
                mid=(best_bid + best_ask) / 2,
            )
        except Exception as exc:
            log.error("orderbook(%s) error: %s", token_id[:12], exc)
            return None

    # --- order placement ---

    async def place_order(
        self,
        market: Market,
        side: str,
        size_usdc: float,
        price: float,
    ) -> Optional[str]:
        if self._paper:
            oid = f"PAPER-{int(time.time()*1000)}"
            log.info(
                "[PAPER] %s %s size=%.2f price=%.4f → %s",
                side, market.question[:60], size_usdc, price, oid,
            )
            return oid

        try:
            oid = await asyncio.get_event_loop().run_in_executor(
                self._executor,
                self._place_live_order_sync,
                market, side, size_usdc, price,
            )
            return oid
        except Exception as exc:
            log.error("place_order live error: %s", exc)
            return None

    def _place_live_order_sync(
        self,
        market: Market,
        side: str,
        size_usdc: float,
        price: float,
    ) -> Optional[str]:
        from py_clob_client.client import ClobClient          # type: ignore
        from py_clob_client.clob_types import OrderArgs, OrderType  # type: ignore

        client = ClobClient(
            host=POLYMARKET_CLOB_URL,
            key=self._private_key,
            chain_id=137,   # Polygon mainnet
        )
        creds = client.create_or_derive_api_creds()
        client = ClobClient(
            host=POLYMARKET_CLOB_URL,
            key=self._private_key,
            chain_id=137,
            api_creds=creds,
            signature_type=0,
        )
        args = OrderArgs(
            token_id=market.token_id,
            price=price,
            size=size_usdc,
            side=side,
            order_type=OrderType.FOK,   # fill-or-kill for latency arb
        )
        resp = client.create_and_post_order(args)
        return resp.get("orderID") if resp else None


# ---------------------------------------------------------------------------
# Helper: extract asset / strike from question text
# ---------------------------------------------------------------------------
def _extract_asset(question: str) -> Optional[str]:
    q = question.upper()
    if "BTC" in q or "BITCOIN" in q:
        return "BTC"
    if "ETH" in q or "ETHEREUM" in q:
        return "ETH"
    return None


def _extract_strike(question: str) -> Optional[float]:
    import re
    for m in re.finditer(r"\$?([\d,]+(?:\.\d+)?)\s*(?:USD|USDC|USDT)?", question):
        val = float(m.group(1).replace(",", ""))
        if val > 100:   # filter out small percentages / counts
            return val
    return None


# ---------------------------------------------------------------------------
# Edge detection  (log-normal probability model)
# ---------------------------------------------------------------------------
class EdgeDetector:
    @staticmethod
    def true_prob(
        spot: float,
        strike: float,
        asset: str,
        tte_secs: float,
    ) -> float:
        """P(spot > strike at expiry) via log-normal model."""
        if tte_secs <= 0:
            return 1.0 if spot > strike else 0.0
        vol = ASSET_VOL.get(asset, 0.90)
        T   = max(tte_secs, 1) / (365.25 * 86_400)
        sqT = math.sqrt(T)
        if sqT < 1e-9:
            return 1.0 if spot > strike else 0.0
        d = (math.log(spot / strike) + 0.5 * vol ** 2 * T) / (vol * sqT)
        return _norm_cdf(d)

    @staticmethod
    def implied_prob(ob: OrderBook) -> float:
        """Mid-market price as the market's implied YES probability."""
        return ob.mid

    @staticmethod
    def best_side(
        true_p: float, implied_p: float
    ) -> Tuple[str, float]:
        """Return (side, edge) for whichever side has the larger edge."""
        buy_edge  = true_p - implied_p            # edge buying YES
        sell_edge = implied_p - (1.0 - true_p)   # edge selling YES (buying NO)
        if buy_edge >= sell_edge:
            return "BUY", buy_edge
        return "SELL", sell_edge


def _norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


# ---------------------------------------------------------------------------
# Position sizing  (half-Kelly with hard cap)
# ---------------------------------------------------------------------------
class Sizer:
    @staticmethod
    def kelly_fraction(edge: float, win_prob: float) -> float:
        """
        Half-Kelly for a binary bet.
        f* = (p - q*p_mkt) / (1 - p_mkt)  ≈ edge / (1 - win_prob + edge)
        Returns half of that.
        """
        if edge <= 0 or win_prob <= 0 or win_prob >= 1:
            return 0.0
        denom = 1.0 - win_prob + edge
        if denom <= 0:
            return 0.0
        return (edge / denom) * KELLY_HALF

    @staticmethod
    def position_usdc(portfolio: float, kelly_f: float) -> float:
        fraction = min(kelly_f, MAX_POSITION_PCT)
        return portfolio * fraction


# ---------------------------------------------------------------------------
# Risk manager
# ---------------------------------------------------------------------------
class RiskManager:
    def __init__(self, tg: Telegram, db: Database) -> None:
        self._tg  = tg
        self._db  = db

        self.halted:            bool          = False
        self.halt_reason:       str           = ""
        self.day_start_balance: float         = 0.0
        self.all_time_high:     float         = 0.0
        self.consec_losses:     int           = 0
        self.pause_until:       Optional[float] = None

    def initialise(self, balance: float) -> None:
        today = date.today().isoformat()
        if self._db.get("risk_day") != today:
            self.day_start_balance = balance
            self._db.set("risk_day", today)
            self._db.set("day_start_balance", str(balance))
        else:
            dsb = self._db.get("day_start_balance")
            self.day_start_balance = float(dsb) if dsb else balance

        ath = self._db.get("all_time_high")
        self.all_time_high = max(float(ath) if ath else balance, balance)
        self._db.set("all_time_high", str(self.all_time_high))

    def _update_ath(self, balance: float) -> None:
        if balance > self.all_time_high:
            self.all_time_high = balance
            self._db.set("all_time_high", str(balance))

    async def check(self, balance: float) -> bool:
        """Return True if it is safe to trade."""
        if self.halted:
            return False

        # Lift pause if it has expired
        if self.pause_until is not None:
            if time.time() >= self.pause_until:
                self.pause_until   = None
                self.consec_losses = 0
                log.info("Consecutive-loss pause lifted — resuming")
            else:
                mins = int((self.pause_until - time.time()) / 60)
                log.info("Trading paused — %d min remaining", mins)
                return False

        self._update_ath(balance)

        # Daily P&L halt
        if self.day_start_balance > 0:
            daily_pnl = (balance - self.day_start_balance) / self.day_start_balance
            if daily_pnl <= DAILY_HALT_THRESHOLD:
                await self._halt(
                    f"Daily halt: P&L = {daily_pnl:.1%} "
                    f"(threshold {DAILY_HALT_THRESHOLD:.0%})"
                )
                return False

        # Permanent drawdown halt
        if self.all_time_high > 0:
            dd = balance / self.all_time_high
            if dd <= DRAWDOWN_HALT_THRESHOLD:
                await self._halt(
                    f"Drawdown halt: portfolio at {dd:.1%} of ATH "
                    f"${self.all_time_high:,.2f}"
                )
                return False

        return True

    async def record_loss(self) -> None:
        self.consec_losses += 1
        if self.consec_losses >= CONSEC_LOSS_PAUSE_N:
            self.pause_until = time.time() + CONSEC_LOSS_PAUSE_SECS
            resume = datetime.utcnow() + timedelta(seconds=CONSEC_LOSS_PAUSE_SECS)
            msg = (
                f"⏸ <b>30-MINUTE PAUSE</b>\n"
                f"{self.consec_losses} consecutive losses.\n"
                f"Resumes at {resume:%H:%M UTC}"
            )
            log.warning("30-min pause triggered after %d consecutive losses", self.consec_losses)
            await self._tg.send(msg)

    def record_win(self) -> None:
        self.consec_losses = 0

    async def _halt(self, reason: str) -> None:
        self.halted      = True
        self.halt_reason = reason
        log.critical("TRADING HALTED: %s", reason)
        await self._tg.send(
            f"🚨 <b>KILL SWITCH TRIGGERED</b>\n"
            f"{reason}\n"
            f"<b>Manual restart required to resume.</b>"
        )


# ---------------------------------------------------------------------------
# Heartbeat  (hourly Telegram summary)
# ---------------------------------------------------------------------------
class Heartbeat:
    def __init__(self, tg: Telegram, db: Database, paper: bool) -> None:
        self._tg    = tg
        self._db    = db
        self._paper = paper
        self._get_balance_fn = None

    def set_balance_fn(self, fn) -> None:
        self._get_balance_fn = fn

    async def run(self) -> None:
        while True:
            await asyncio.sleep(3_600)
            await self._ping()

    async def _ping(self) -> None:
        try:
            balance = await self._get_balance_fn() if self._get_balance_fn else 0.0
            trades  = self._db.today_closed_trades()
            total   = len(trades)
            wins    = sum(1 for t in trades if t["pnl"] is not None and t["pnl"] > 0)
            wr      = wins / total if total else 0.0
            mode    = "PAPER" if self._paper else "LIVE"
            await self._tg.send(
                f"💓 <b>Heartbeat [{mode}]</b>\n"
                f"Balance: <b>${balance:,.2f}</b>\n"
                f"Today — trades: {total} | wins: {wins} | win rate: {wr:.0%}\n"
                f"{datetime.utcnow():%Y-%m-%d %H:%M UTC}"
            )
        except Exception as exc:
            log.error("Heartbeat error: %s", exc)


# ---------------------------------------------------------------------------
# Bot  (main orchestrator)
# ---------------------------------------------------------------------------
class PolyBot:
    def __init__(self, cfg: Dict[str, str], paper: bool) -> None:
        self._paper = paper
        self._db    = Database()
        self._tg    = Telegram(cfg["TELEGRAM_TOKEN"], cfg["TELEGRAM_CHAT_ID"])
        self._feed  = BinanceFeed()
        self._poly  = PolyClient(
            api_key=cfg["POLY_API_KEY"],
            private_key=cfg["POLY_PRIVATE_KEY"],
            rpc_url=cfg["ALCHEMY_RPC_URL"],
            paper=paper,
        )
        self._risk  = RiskManager(self._tg, self._db)
        self._hb    = Heartbeat(self._tg, self._db, paper)
        self._open: Dict[str, dict] = {}   # token_id → position dict

    # --- lifecycle ---

    async def _start(self) -> None:
        self._db.connect()
        await self._tg.start()
        await self._poly.start()

        balance = await self._get_balance()
        self._risk.initialise(balance)
        self._hb.set_balance_fn(self._get_balance)

        mode = "PAPER" if self._paper else "LIVE"
        log.info("PolyBot starting in %s mode — balance $%.2f", mode, balance)
        await self._tg.send(
            f"🤖 <b>PolyBot started [{mode}]</b>\n"
            f"Balance: ${balance:,.2f}\n"
            f"{datetime.utcnow():%Y-%m-%d %H:%M UTC}"
        )

    async def _stop(self) -> None:
        self._feed.stop()
        await self._poly.stop()
        await self._tg.stop()
        self._db.close()

    async def run(self) -> None:
        await self._start()
        tasks = [
            asyncio.create_task(self._feed.run(),       name="binance-feed"),
            asyncio.create_task(self._scan_loop(),      name="scan-loop"),
            asyncio.create_task(self._manage_loop(),    name="manage-positions"),
            asyncio.create_task(self._hb.run(),         name="heartbeat"),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            log.info("Tasks cancelled — shutting down")
        finally:
            await self._stop()

    # --- balance helper ---

    async def _get_balance(self) -> float:
        if self._paper:
            raw = self._db.get("paper_balance", "10000.0")
            return float(raw)  # type: ignore[arg-type]
        return await self._poly.balance()

    async def _adjust_paper_balance(self, delta: float) -> None:
        bal = await self._get_balance()
        self._db.set("paper_balance", str(bal + delta))

    # --- scan loop ---

    async def _scan_loop(self) -> None:
        await asyncio.sleep(4)   # let price feed warm up
        while True:
            try:
                await self._scan_once()
            except Exception as exc:
                log.error("Scan error: %s", exc)
                await self._tg.send(f"⚠️ Scan error: {exc}")
            await asyncio.sleep(1)

    async def _scan_once(self) -> None:
        if self._risk.halted:
            await asyncio.sleep(10)
            return

        # Pause if ALL prices are stale
        btc_stale = await self._feed.is_stale("BTC")
        eth_stale = await self._feed.is_stale("ETH")
        if btc_stale and eth_stale:
            log.warning("All prices stale — pausing scan")
            await asyncio.sleep(2)
            return

        portfolio = await self._get_balance()
        if not await self._risk.check(portfolio):
            await asyncio.sleep(5)
            return

        t0      = time.monotonic()
        markets = await self._poly.get_markets()
        log.debug(
            "Fetched %d markets in %.0f ms",
            len(markets), (time.monotonic() - t0) * 1000,
        )

        for mkt in markets:
            if mkt.token_id in self._open:
                continue

            spot = await self._feed.price(mkt.asset)
            if spot is None:
                continue

            ob = await self._poly.orderbook(mkt.token_id)
            if ob is None:
                continue

            now     = datetime.now(timezone.utc)
            tte_s   = max((mkt.expiry - now).total_seconds(), 1.0)
            true_p  = EdgeDetector.true_prob(spot, mkt.strike, mkt.asset, tte_s)
            impl_p  = EdgeDetector.implied_prob(ob)
            side, edge = EdgeDetector.best_side(true_p, impl_p)

            if edge < MIN_DETECTABLE_EDGE:
                continue

            log.info(
                "Edge %.1f%% | %s | side=%s | true=%.3f impl=%.3f",
                edge * 100, mkt.question[:55], side, true_p, impl_p,
            )

            if edge < MIN_EXECUTION_EDGE:
                continue   # monitor only

            win_p   = true_p if side == "BUY" else 1.0 - true_p
            kelly_f = Sizer.kelly_fraction(edge, win_p)
            size    = Sizer.position_usdc(portfolio, kelly_f)

            if size < 1.0:
                log.debug("Size too small (%.2f) — skipping", size)
                continue

            entry_px = ob.best_ask if side == "BUY" else ob.best_bid

            t_exec = time.monotonic()
            oid    = await self._poly.place_order(mkt, side, size, entry_px)
            exec_ms = (time.monotonic() - t_exec) * 1000

            if oid is None:
                log.error("Order failed for %s", mkt.condition_id)
                await self._tg.send(f"⚠️ Order failed: {mkt.question[:60]}")
                continue

            if exec_ms > EXECUTION_TARGET_MS:
                log.warning("Execution %.0f ms exceeded %d ms target", exec_ms, EXECUTION_TARGET_MS)

            db_id = None
            if self._paper:
                db_id = self._db.trade_open(
                    mkt.condition_id, mkt.asset, side, size, entry_px, edge,
                )

            self._open[mkt.token_id] = {
                "market":   mkt,
                "oid":      oid,
                "side":     side,
                "entry_px": entry_px,
                "size":     size,
                "edge":     edge,
                "db_id":    db_id,
            }

            icon = "📄" if self._paper else "🟢"
            await self._tg.send(
                f"{icon} <b>Trade Entry</b>\n"
                f"{mkt.question[:80]}\n"
                f"Side: <b>{side}</b> | Size: <b>${size:.2f}</b>\n"
                f"Entry: {entry_px:.4f} | Edge: {edge:.1%}\n"
                f"Exec: {exec_ms:.0f} ms | OID: {oid}"
            )
            log.info(
                "Opened %s %s size=%.2f entry=%.4f edge=%.1f%% exec=%.0fms",
                side, mkt.question[:50], size, entry_px, edge * 100, exec_ms,
            )

    # --- position management loop ---

    async def _manage_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            for token_id in list(self._open):
                await self._maybe_close(token_id)

    async def _maybe_close(self, token_id: str) -> None:
        pos = self._open.get(token_id)
        if pos is None:
            return
        mkt: Market = pos["market"]
        now  = datetime.now(timezone.utc)
        tte  = (mkt.expiry - now).total_seconds()

        should_close = False

        if tte <= 30:
            should_close = True   # approaching expiry
        else:
            spot = await self._feed.price(mkt.asset)
            ob   = await self._poly.orderbook(token_id)
            if spot is not None and ob is not None:
                true_p = EdgeDetector.true_prob(spot, mkt.strike, mkt.asset, max(tte, 1))
                impl_p = EdgeDetector.implied_prob(ob)
                # Edge has reversed — exit early
                if pos["side"] == "BUY"  and impl_p >= true_p + 0.03:
                    should_close = True
                elif pos["side"] == "SELL" and impl_p <= true_p - 0.03:
                    should_close = True

        if should_close:
            await self._close(token_id)

    async def _close(self, token_id: str) -> None:
        pos = self._open.pop(token_id, None)
        if pos is None:
            return
        mkt: Market = pos["market"]

        ob       = await self._poly.orderbook(token_id)
        exit_px  = ob.mid if ob else pos["entry_px"]
        close_side = "SELL" if pos["side"] == "BUY" else "BUY"
        await self._poly.place_order(mkt, close_side, pos["size"], exit_px)

        if pos["side"] == "BUY":
            pnl = pos["size"] * (exit_px - pos["entry_px"]) / pos["entry_px"]
        else:
            pnl = pos["size"] * (pos["entry_px"] - exit_px) / pos["entry_px"]

        if self._paper:
            await self._adjust_paper_balance(pnl)
            if pos["db_id"]:
                self._db.trade_close(pos["db_id"], exit_px, pnl)

        if pnl >= 0:
            self._risk.record_win()
        else:
            await self._risk.record_loss()

        result = "WIN" if pnl >= 0 else "LOSS"
        icon   = "🔵" if pnl >= 0 else "🔴"
        icon   = "📄" if self._paper else icon
        await self._tg.send(
            f"{icon} <b>Trade Exit [{result}]</b>\n"
            f"{mkt.question[:80]}\n"
            f"P&L: <b>${pnl:+.2f}</b>\n"
            f"Entry: {pos['entry_px']:.4f} → Exit: {exit_px:.4f}"
        )
        log.info(
            "Closed %s %s pnl=%.2f entry=%.4f exit=%.4f",
            pos["side"], mkt.question[:50], pnl, pos["entry_px"], exit_px,
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Polymarket Latency Arbitrage Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
  Paper mode (default):
      python bot.py

  Live trading (all three flags required):
      python bot.py --live --confirm --i-understand-risks

  WARNING: Live trading uses real funds on Polygon / Polymarket.
""",
    )
    p.add_argument("--live",               action="store_true",
                   help="Enable live trading")
    p.add_argument("--confirm",            action="store_true",
                   help="Confirm live trading intent")
    p.add_argument("--i-understand-risks", action="store_true",
                   dest="i_understand_risks",
                   help="Acknowledge financial risk")
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    live_enabled = args.live and args.confirm and args.i_understand_risks

    if args.live and not live_enabled:
        print(
            "ERROR: Live trading requires ALL three flags:\n"
            "  --live --confirm --i-understand-risks\n"
            "Falling back to PAPER mode.\n"
        )

    paper = not live_enabled

    if not paper:
        print("=" * 60)
        print("  *** LIVE TRADING MODE ***")
        print("  Real funds will be used.  Press Ctrl-C to abort.")
        print("=" * 60)
        time.sleep(5)

    try:
        cfg = validate_env()
    except EnvironmentError as exc:
        print(f"Configuration error: {exc}")
        sys.exit(1)

    log.info("Starting PolyBot — mode=%s", "PAPER" if paper else "LIVE")

    bot  = PolyBot(cfg, paper=paper)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _on_signal(sig, _frame):
        log.info("Signal %s received — cancelling tasks", sig)
        for t in asyncio.all_tasks(loop):
            t.cancel()

    signal.signal(signal.SIGINT,  _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        loop.run_until_complete(bot.run())
    except KeyboardInterrupt:
        log.info("Keyboard interrupt")
    finally:
        loop.close()
        log.info("PolyBot stopped")


if __name__ == "__main__":
    main()
