"""Resumable, auditable full-market close-history backfill."""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field


class MarketHistoryBackfillReport(BaseModel):
    strategy_version: str = "market_sentiment_history_v1"
    dataset: str = "market_history_backfill"
    status: str
    input_hash: str
    promotion_eligible: bool = False
    started_at: str
    finished_at: str
    requested_sessions: int
    session_dates: list[str] = Field(default_factory=list)
    universe_count: int = 0
    completed_symbols: int = 0
    processed_symbols: int = 0
    remaining_symbols: int = 0
    newly_completed_symbols: int = 0
    priority_expected_count: int = 0
    priority_symbol_count: int = 0
    priority_completed_symbols: int = 0
    priority_missing_symbols: list[str] = Field(default_factory=list)
    batch_limit: int | None = None
    failed_symbols: int = 0
    coverage_by_date: dict[str, int] = Field(default_factory=dict)
    data_gaps: list[str] = Field(default_factory=list)
    failure_examples: list[str] = Field(default_factory=list)
    source_details: dict[str, Any] = Field(default_factory=dict)


class MarketHistoryBackfillService:
    """Backfill real closes for the latest completed A-share sessions.

    Existing complete symbols are skipped, making every invocation resumable.
    No row is generated for a missing symbol/day and no coverage threshold is
    relaxed. The backfill is evidence for live sentiment inputs, never direct
    strategy-promotion evidence.
    """

    def __init__(
        self,
        *,
        universe_loader: Callable[..., list[dict[str, Any]]] | None = None,
        daily_loader: Callable[..., list[dict[str, Any]]] | None = None,
        session_loader: Callable[..., list[dict[str, Any]]] | None = None,
        priority_symbol_loader: Callable[[], set[str]] | None = None,
        requested_sessions: int = 21,
        minimum_daily_coverage: int = 3000,
        request_pause_seconds: float = 0.02,
        max_symbols_per_run: int | None = 400,
    ) -> None:
        if universe_loader is None or daily_loader is None or session_loader is None:
            from pa_agent.data.eastmoney_client import (
                fetch_index_daily,
                fetch_stock_daily_recent,
                iter_stock_universe,
            )

            universe_loader = universe_loader or iter_stock_universe
            daily_loader = daily_loader or fetch_stock_daily_recent
            session_loader = session_loader or fetch_index_daily
        self.universe_loader = universe_loader
        self.daily_loader = daily_loader
        self.session_loader = session_loader
        self.priority_symbol_loader = priority_symbol_loader or _load_hs300_symbols
        self.priority_expected_count = 300 if priority_symbol_loader is None else 0
        self.requested_sessions = max(21, int(requested_sessions))
        self.minimum_daily_coverage = max(3000, int(minimum_daily_coverage))
        self.request_pause_seconds = max(0.0, float(request_pause_seconds))
        self.max_symbols_per_run = (
            None
            if max_symbols_per_run is None
            else max(1, int(max_symbols_per_run))
        )
        self._universe_cache: list[dict[str, Any]] = []
        self._universe_cached_at = 0.0
        self._priority_symbols_cache: set[str] | None = None

    def backfill(
        self,
        *,
        store: Any,
        captured_at: datetime | None = None,
        progress: Callable[[int, int, str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> MarketHistoryBackfillReport:
        now = (captured_at or datetime.now().astimezone()).astimezone()
        started_at = now.isoformat()
        session_dates = self._closed_session_dates(now)
        rows = self._load_universe()
        priority = self._priority_symbols()
        symbols = sorted({
            str(item.get("code") or "")
            for item in rows
            if str(item.get("code") or "").isdigit()
            and len(str(item.get("code") or "")) == 6
        } | priority)
        symbol_set = set(symbols)
        already_complete = store.complete_market_history_symbols(
            symbol_set,
            session_dates=session_dates,
        )
        completed_before = len(already_complete)
        failures: list[str] = []
        completed = len(already_complete)
        total = len(symbols)
        target_dates = set(session_dates)
        pending = [symbol for symbol in symbols if symbol not in already_complete]
        pending = self._resume_order(store, pending)
        pending = (
            [symbol for symbol in pending if symbol in priority]
            + [symbol for symbol in pending if symbol not in priority]
        )
        batch = (
            pending
            if self.max_symbols_per_run is None
            else pending[: self.max_symbols_per_run]
        )
        processed = 0
        last_processed_symbol = ""
        for symbol in batch:
            if cancel_check and cancel_check():
                break
            processed += 1
            last_processed_symbol = symbol
            try:
                bars = self.daily_loader(
                    symbol,
                    n=max(35, self.requested_sessions + 10),
                    adjust="none",
                )
                verified = []
                for bar in bars:
                    value = bar.get("time")
                    as_of = (
                        value.date().isoformat()
                        if isinstance(value, datetime)
                        else str(value)[:10]
                    )
                    close = bar.get("close")
                    if as_of in target_dates and close is not None and float(close) > 0:
                        verified.append({
                            "as_of": as_of,
                            "symbol": symbol,
                            "price": float(close),
                        })
                store.upsert_market_daily_price_rows(
                    verified,
                    captured_at=now.isoformat(),
                )
                if len({item["as_of"] for item in verified}) == len(target_dates):
                    completed += 1
                else:
                    failures.append(
                        f"{symbol}:sessions_{len({item['as_of'] for item in verified})}"
                    )
            except Exception as exc:  # noqa: BLE001 - recorded and resumed later
                failures.append(f"{symbol}:{type(exc).__name__}")
            if progress:
                progress(processed, len(batch), symbol)
            if self.request_pause_seconds:
                time.sleep(self.request_pause_seconds)

        coverage = store.market_daily_price_coverage(session_dates=session_dates)
        priority_complete = store.complete_market_history_symbols(
            priority,
            session_dates=session_dates,
        )
        priority_missing = sorted(priority - priority_complete)
        gaps = []
        if self.priority_expected_count and len(priority) != self.priority_expected_count:
            gaps.append(
                f"hs300_priority_source_{len(priority)}_of_"
                f"{self.priority_expected_count}"
            )
        if len(session_dates) != self.requested_sessions:
            gaps.append(
                f"closed_session_calendar_{len(session_dates)}_of_{self.requested_sessions}"
            )
        for day in session_dates:
            count = int(coverage.get(day, 0))
            if count < self.minimum_daily_coverage:
                gaps.append(
                    f"market_history_coverage_{day}_{count}_below_"
                    f"{self.minimum_daily_coverage}"
                )
        if priority_missing:
            gaps.append(
                f"hs300_priority_history_{len(priority_complete)}_of_{len(priority)}"
            )
        cancelled = bool(cancel_check and cancel_check())
        if cancelled:
            gaps.append("market_history_backfill_cancelled")
        remaining = max(0, total - completed)
        coverage_incomplete = any(
            gap.startswith((
                "closed_session_calendar_",
                "market_history_coverage_",
                "hs300_priority_history_",
                "hs300_priority_source_",
            ))
            for gap in gaps
        )
        if remaining and self.max_symbols_per_run is not None and coverage_incomplete:
            gaps.append(f"market_history_batch_remaining:{remaining}")
        payload = {
            "session_dates": session_dates,
            "symbols": symbols,
            "batch_symbols": batch,
            "last_processed_symbol": last_processed_symbol,
            "coverage": coverage,
            "priority_symbols": sorted(priority),
            "priority_missing_symbols": priority_missing,
            "source": "eastmoney_clist_and_unadjusted_daily_kline",
        }
        input_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return MarketHistoryBackfillReport(
            status="complete" if not gaps else "data_incomplete",
            input_hash=input_hash,
            started_at=started_at,
            finished_at=datetime.now().astimezone().isoformat(),
            requested_sessions=self.requested_sessions,
            session_dates=session_dates,
            universe_count=total,
            completed_symbols=completed,
            processed_symbols=processed,
            remaining_symbols=remaining,
            newly_completed_symbols=max(0, completed - completed_before),
            priority_expected_count=self.priority_expected_count,
            priority_symbol_count=len(priority),
            priority_completed_symbols=len(priority_complete),
            priority_missing_symbols=priority_missing[:300],
            batch_limit=self.max_symbols_per_run,
            failed_symbols=len(failures),
            coverage_by_date=coverage,
            data_gaps=gaps,
            failure_examples=failures[:30],
            source_details={
                "universe_source": "East Money A-share clist",
                "price_source": "East Money unadjusted daily kline",
                "captured_at": now.isoformat(),
                "resumable": True,
                "last_processed_symbol": last_processed_symbol,
                "minimum_daily_coverage": self.minimum_daily_coverage,
                "priority_dependency": "official_current_hs300",
            },
        )

    def _load_universe(self) -> list[dict[str, Any]]:
        """Reuse the same real intraday universe across resumable batches."""
        cache_age = time.monotonic() - self._universe_cached_at
        if self._universe_cache and cache_age < 6 * 60 * 60:
            return [dict(item) for item in self._universe_cache]
        rows = list(self.universe_loader())
        self._universe_cache = [dict(item) for item in rows]
        self._universe_cached_at = time.monotonic()
        return rows

    def _priority_symbols(self) -> set[str]:
        """Complete hard sentiment dependencies before the rest of full A shares."""
        if self._priority_symbols_cache is not None:
            return set(self._priority_symbols_cache)
        try:
            symbols = {
                str(symbol)
                for symbol in self.priority_symbol_loader()
                if str(symbol).isdigit() and len(str(symbol)) == 6
            }
            self._priority_symbols_cache = symbols
        except Exception:  # noqa: BLE001 - backfill continues but remains fail-closed
            self._priority_symbols_cache = set()
        return set(self._priority_symbols_cache)

    @staticmethod
    def _resume_order(store: Any, pending: list[str]) -> list[str]:
        """Continue after the prior batch, then wrap to retry incomplete symbols."""
        if not pending:
            return []
        runs = store.list_validation_runs(
            strategy_version="market_sentiment_history_v1",
            limit=1,
        )
        previous = (runs[0].get("report") or {}) if runs else {}
        cursor = str((previous.get("source_details") or {}).get("last_processed_symbol") or "")
        if not cursor:
            return pending
        after = [symbol for symbol in pending if symbol > cursor]
        before = [symbol for symbol in pending if symbol <= cursor]
        return after + before

    def _closed_session_dates(self, now: datetime) -> list[str]:
        start = (now.date() - timedelta(days=90)).strftime("%Y%m%d")
        end = now.strftime("%Y%m%d")
        rows = self.session_loader("000001", start_date=start, end_date=end)
        dates = []
        for item in rows:
            value = item.get("time")
            day = (
                value.date()
                if isinstance(value, datetime)
                else datetime.fromisoformat(str(value)[:10]).date()
            )
            if day > now.date() or (day == now.date() and now.hour < 15):
                continue
            dates.append(day.isoformat())
        return sorted(set(dates))[-self.requested_sessions :]


def _load_hs300_symbols() -> set[str]:
    from pa_agent.trading.universe import load_official_current_hs300

    return {item.symbol for item in load_official_current_hs300().constituents}
