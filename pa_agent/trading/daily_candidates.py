"""Production scanner for deterministic daily candidates.

The scanner is deliberately separate from the GUI and AI layers.  It freezes
one closed-daily data set for the current universe, computes pool breadth,
evaluates every member with the same strategy implementation used by replay,
and persists one idempotent decision per symbol/date/version.
"""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta
from typing import Any

from pydantic import BaseModel, Field

from pa_agent.data.base import KlineBar
from pa_agent.trading.quant import Hs300DailyPullbackStrategy, SignalDecision, StrategyContext


class DailyCandidateScanResult(BaseModel):
    pool_version: str
    signal_date: date | None = None
    market_breadth_pct: float | None = None
    decisions: list[SignalDecision] = Field(default_factory=list)
    data_complete: bool = True
    data_gaps: list[str] = Field(default_factory=list)

    @property
    def allowed(self) -> list[SignalDecision]:
        return [item for item in self.decisions if item.status.value == "allow"]


class DailyCandidateScanner:
    """Scan the latest common closed day for the active fixed trading pool."""

    def __init__(
        self,
        strategy: Hs300DailyPullbackStrategy,
        *,
        stock_daily_loader: Callable[..., list[dict[str, Any]]] | None = None,
        index_daily_loader: Callable[..., list[dict[str, Any]]] | None = None,
        max_workers: int = 6,
    ) -> None:
        if stock_daily_loader is None or index_daily_loader is None:
            from pa_agent.data.eastmoney_client import fetch_index_daily, fetch_stock_daily_recent

            stock_daily_loader = stock_daily_loader or fetch_stock_daily_recent
            index_daily_loader = index_daily_loader or fetch_index_daily
        self.strategy = strategy
        self.stock_daily_loader = stock_daily_loader
        self.index_daily_loader = index_daily_loader
        self.max_workers = max_workers

    def scan(
        self,
        pool_snapshot: dict[str, Any],
        *,
        captured_at: datetime | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> DailyCandidateScanResult:
        now = (captured_at or datetime.now().astimezone()).astimezone()
        pool_version = str(pool_snapshot.get("version") or "")
        symbols = [str(item) for item in pool_snapshot.get("symbols") or []]
        member_by_symbol = {
            str(item.get("symbol") or ""): item
            for item in pool_snapshot.get("members") or []
        }
        tradable_symbols = [
            symbol for symbol in symbols
            if bool((member_by_symbol.get(symbol) or {}).get("authorization_eligible", True))
        ]
        if not pool_snapshot.get("data_complete", True):
            return DailyCandidateScanResult(
                pool_version=pool_version,
                data_complete=False,
                data_gaps=["universe_snapshot_incomplete"],
            )
        if not pool_version or not symbols:
            return DailyCandidateScanResult(
                pool_version=pool_version,
                data_complete=False,
                data_gaps=["missing_current_universe"],
            )

        stock_rows: dict[str, list[dict[str, Any]]] = {}
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self.stock_daily_loader, symbol, n=90, adjust="qfq"): symbol
                for symbol in tradable_symbols
            }
            for completed, future in enumerate(as_completed(futures), 1):
                symbol = futures[future]
                try:
                    rows = _closed_daily_rows(future.result(), now)
                    if len(rows) < 65:
                        errors.append(f"stock_{symbol}_requires_65_closed_daily_bars")
                    else:
                        stock_rows[symbol] = rows
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"stock_{symbol}_fetch_failed:{type(exc).__name__}")
                if progress is not None:
                    progress(completed, len(tradable_symbols), symbol)

        if errors or len(stock_rows) != len(tradable_symbols):
            return DailyCandidateScanResult(
                pool_version=pool_version,
                data_complete=False,
                data_gaps=list(dict.fromkeys(errors or ["stock_daily_data_incomplete"])),
            )
        if not tradable_symbols:
            return DailyCandidateScanResult(
                pool_version=pool_version,
                data_complete=False,
                data_gaps=["no_authorization_eligible_universe_members"],
            )
        signal_day = min(rows[-1]["_date"] for rows in stock_rows.values())
        if any(rows[-1]["_date"] != signal_day for rows in stock_rows.values()):
            return DailyCandidateScanResult(
                pool_version=pool_version,
                signal_date=signal_day,
                data_complete=False,
                data_gaps=["stock_pool_latest_closed_day_mismatch"],
            )

        start = (signal_day - timedelta(days=150)).strftime("%Y%m%d")
        end = signal_day.strftime("%Y%m%d")
        try:
            index_rows = _closed_daily_rows(
                self.index_daily_loader("000300", start_date=start, end_date=end), now
            )
        except Exception as exc:  # noqa: BLE001
            return DailyCandidateScanResult(
                pool_version=pool_version,
                signal_date=signal_day,
                data_complete=False,
                data_gaps=[f"hs300_daily_fetch_failed:{type(exc).__name__}"],
            )
        index_rows = [item for item in index_rows if item["_date"] <= signal_day]
        if len(index_rows) < 65 or index_rows[-1]["_date"] != signal_day:
            return DailyCandidateScanResult(
                pool_version=pool_version,
                signal_date=signal_day,
                data_complete=False,
                data_gaps=["hs300_requires_65_bars_aligned_to_signal_day"],
            )

        above_ma20 = 0
        for rows in stock_rows.values():
            closes = [float(item["close"]) for item in rows]
            if closes[-1] > sum(closes[-20:]) / 20:
                above_ma20 += 1
        breadth = above_ma20 / len(tradable_symbols) * 100
        signal_time = datetime.combine(signal_day, time(15, 0), tzinfo=now.tzinfo).isoformat()
        next_time = datetime.combine(_next_weekday(signal_day), time(15, 0), tzinfo=now.tzinfo).isoformat()
        index_bars = _to_bars(index_rows)
        decisions = []
        for symbol in tradable_symbols:
            member = member_by_symbol.get(symbol) or {}
            eligibility_reasons = tuple(member.get("eligibility_reasons") or ())
            decisions.append(self.strategy.evaluate(StrategyContext(
                symbol=symbol,
                bars=_to_bars(stock_rows[symbol]),
                index_bars=index_bars,
                market_breadth_pct=round(breadth, 6),
                pool_version=pool_version,
                signal_time=signal_time,
                next_trading_time=next_time,
                eligible=bool(member.get("authorization_eligible", True)),
                eligibility_reasons=eligibility_reasons,
            )))
        return DailyCandidateScanResult(
            pool_version=pool_version,
            signal_date=signal_day,
            market_breadth_pct=round(breadth, 6),
            decisions=decisions,
        )


def _closed_daily_rows(rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        value = row.get("time")
        timestamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=now.tzinfo)
        day = timestamp.date()
        if day > now.date() or (day == now.date() and now.time() < time(15, 0)):
            continue
        result.append({**row, "_timestamp": timestamp, "_date": day})
    return sorted(result, key=lambda item: item["_timestamp"])


def _to_bars(rows: list[dict[str, Any]]) -> tuple[KlineBar, ...]:
    size = len(rows)
    return tuple(KlineBar(
        seq=size - index,
        ts_open=item["_timestamp"].timestamp() * 1000,
        open=float(item["open"]),
        high=float(item["high"]),
        low=float(item["low"]),
        close=float(item["close"]),
        volume=float(item.get("volume") or 0),
        amount=float(item.get("amount") or 0),
        pct_chg=(float(item["pct_chg"]) if item.get("pct_chg") is not None else None),
        closed=True,
    ) for index, item in enumerate(rows))


def _next_weekday(day: date) -> date:
    result = day + timedelta(days=1)
    while result.weekday() >= 5:
        result += timedelta(days=1)
    return result
