"""Background closed-daily synchronization for open trade lifecycles."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from pa_agent.data.base import KlineBar


class LifecycleMarketDataSync:
    def __init__(
        self,
        store: Any,
        tracker: Any,
        *,
        daily_loader: Callable[..., list[dict[str, Any]]] | None = None,
    ) -> None:
        if daily_loader is None:
            from pa_agent.data.eastmoney_client import fetch_stock_daily_recent

            daily_loader = fetch_stock_daily_recent
        self.store = store
        self.tracker = tracker
        self.daily_loader = daily_loader

    def sync_open_daily(self, *, now: datetime | None = None) -> dict[str, Any]:
        captured_at = (now or datetime.now().astimezone()).astimezone()
        symbols = sorted({
            str(plan.get("symbol") or "")
            for plan in self.store.list_plans(lifecycle_open=True)
            if str(plan.get("symbol") or "").isdigit()
            and str((plan.get("risk_snapshot") or {}).get("management_timeframe") or plan.get("timeframe"))
            == "1d"
        })
        processed_events = 0
        failures: dict[str, str] = {}
        for symbol in symbols:
            try:
                rows = self.daily_loader(symbol, n=40, adjust="qfq")
                for bar in _closed_daily_bars(rows, captured_at):
                    processed_events += len(self.tracker.process_closed_bar(
                        symbol=symbol,
                        timeframe="1d",
                        bar=bar,
                    ))
            except Exception as exc:  # noqa: BLE001
                failures[symbol] = f"{type(exc).__name__}:{exc}"
        return {
            "captured_at": captured_at.isoformat(),
            "symbols": symbols,
            "event_count": processed_events,
            "failures": failures,
        }


def _closed_daily_bars(
    rows: list[dict[str, Any]], now: datetime
) -> list[KlineBar]:
    result: list[tuple[datetime, KlineBar]] = []
    for row in rows:
        raw_time = row.get("time")
        try:
            timestamp = (
                raw_time if isinstance(raw_time, datetime)
                else datetime.fromisoformat(str(raw_time))
            )
        except (TypeError, ValueError):
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=now.tzinfo)
        else:
            timestamp = timestamp.astimezone(now.tzinfo)
        if timestamp.date() > now.date():
            continue
        if timestamp.date() == now.date() and now.hour < 15:
            continue
        try:
            bar = KlineBar(
                seq=1,
                ts_open=timestamp.timestamp() * 1000,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume") or 0),
                amount=float(row.get("amount") or 0),
                closed=True,
            )
        except (KeyError, TypeError, ValueError):
            continue
        result.append((timestamp, bar))
    return [bar for _, bar in sorted(result, key=lambda item: item[0])]
