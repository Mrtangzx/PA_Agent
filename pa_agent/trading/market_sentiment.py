"""Structured, fail-closed A-share market sentiment snapshots."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from pa_agent.trading.topdown import SentimentScoreInput


class MarketSentimentSnapshot(BaseModel):
    captured_at: str
    source_as_of: str = ""
    input: SentimentScoreInput | None = None
    data_complete: bool = True
    data_gaps: list[str] = Field(default_factory=list)
    source_details: dict[str, Any] = Field(default_factory=dict)
    source_hash: str = ""

    def with_source_hash(self) -> MarketSentimentSnapshot:
        payload = self.model_dump(mode="json", exclude={"source_hash"})
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return self.model_copy(update={"source_hash": hashlib.sha256(encoded).hexdigest()})


class MarketSentimentService:
    """Build the 30-point input from structured East Money market data.

    Metrics that cannot currently be sourced with the required timestamp are
    never estimated: the snapshot is marked incomplete and the scorer fails
    closed.  The available counts and breadth remain visible for diagnosis.
    """

    def __init__(
        self,
        *,
        universe_loader: Callable[..., list[dict[str, Any]]] | None = None,
        limit_pool_loader: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        if universe_loader is None or limit_pool_loader is None:
            from pa_agent.data.eastmoney_client import iter_stock_universe

            universe_loader = universe_loader or iter_stock_universe
            limit_pool_loader = limit_pool_loader or fetch_limit_pool_snapshot
        self.universe_loader = universe_loader
        self.limit_pool_loader = limit_pool_loader

    def capture(
        self,
        *,
        hs300_breadth_pct: float | None,
        captured_at: datetime | None = None,
        historical_turnover_vs_ma20: float | None = None,
        new_high_count: int | None = None,
        new_low_count: int | None = None,
        previous_inputs: list[SentimentScoreInput] | None = None,
        broad_index_positive: bool | None = None,
        market_rows: list[dict[str, Any]] | None = None,
    ) -> MarketSentimentSnapshot:
        now = (captured_at or datetime.now().astimezone()).astimezone()
        gaps: list[str] = []
        rows = market_rows if market_rows is not None else self.universe_loader()
        valid = [item for item in rows if item.get("pct_chg") is not None]
        if not rows or len(valid) < 3000:
            gaps.append("a_share_universe_incomplete")
        advancing_pct = (
            sum(float(item["pct_chg"]) > 0 for item in valid) / len(valid) * 100
            if valid else 0.0
        )
        a_share_turnover = sum(float(item.get("amount") or 0) for item in valid)
        pool = self.limit_pool_loader(date=now.strftime("%Y%m%d"))
        limit_up = list(pool.get("limit_up") or [])
        limit_down = list(pool.get("limit_down") or [])
        blast = list(pool.get("blast") or [])
        up_count = int(pool.get("limit_up_count") or len(limit_up))
        down_count = int(pool.get("limit_down_count") or len(limit_down))
        blast_count = int(pool.get("blast_count") or len(blast))
        if not pool.get("source_as_of"):
            gaps.append("limit_pool_timestamp_missing")
        attempted = up_count + blast_count
        seal_success = up_count / attempted * 100 if attempted else 100.0
        blast_pct = blast_count / attempted * 100 if attempted else 0.0
        if historical_turnover_vs_ma20 is None:
            gaps.append("turnover_ma20_history_missing")
        if new_high_count is None or new_low_count is None:
            gaps.append("new_high_low_counts_missing")
        if hs300_breadth_pct is None:
            gaps.append("hs300_breadth_missing")
        if gaps:
            return MarketSentimentSnapshot(
                captured_at=now.isoformat(),
                source_as_of=str(pool.get("source_as_of") or ""),
                data_complete=False,
                data_gaps=gaps,
                source_details={
                    "a_share_count": len(valid),
                    "advancing_pct": round(advancing_pct, 6),
                    "limit_up_count": up_count,
                    "limit_down_count": down_count,
                    "blast_count": blast_count,
                    "seal_success_pct": round(seal_success, 6),
                    "blast_board_pct": round(blast_pct, 6),
                    "a_share_turnover": a_share_turnover,
                },
            ).with_source_hash()
        previous = sorted(
            previous_inputs or [], key=lambda item: item.captured_at, reverse=True
        )
        retreat_now = (
            advancing_pct < 40
            or hs300_breadth_pct is None
            or hs300_breadth_pct < 40
        )
        retreat_bars = 0
        if retreat_now:
            retreat_bars = 1
            if previous:
                retreat_bars = max(1, previous[0].retreat_or_panic_bars + 1)
        worsening = bool(
            previous
            and down_count > previous[0].limit_down_count
            and blast_pct > previous[0].blast_board_pct
        )
        index_positive = (
            advancing_pct >= 50
            if broad_index_positive is None else bool(broad_index_positive)
        )
        systemic_selloff = bool(
            historical_turnover_vs_ma20 >= 1.2
            and not index_positive
            and (
                advancing_pct < 40
                or hs300_breadth_pct is None
                or hs300_breadth_pct < 40
            )
        )
        return MarketSentimentSnapshot(
            captured_at=now.isoformat(),
            source_as_of=str(pool["source_as_of"]),
            input=SentimentScoreInput(
                advancing_pct=advancing_pct,
                hs300_above_ma20_pct=float(hs300_breadth_pct),
                limit_up_count=up_count,
                limit_down_count=down_count,
                seal_success_pct=seal_success,
                blast_board_pct=blast_pct,
                new_high_count=new_high_count,
                new_low_count=new_low_count,
                turnover_vs_ma20=historical_turnover_vs_ma20,
                broad_index_positive=index_positive,
                retreat_or_panic_bars=retreat_bars,
                limit_down_and_blast_worsening=worsening,
                systemic_volume_selloff=systemic_selloff,
                captured_at=now.isoformat(),
            ),
            source_details={
                "a_share_count": len(valid),
                "a_share_turnover": a_share_turnover,
                "blast_count": blast_count,
            },
        ).with_source_hash()

    def capture_for_store(
        self,
        *,
        store: Any,
        hs300_breadth_pct: float | None,
        captured_at: datetime | None = None,
    ) -> MarketSentimentSnapshot:
        """Capture all structured inputs once and persist the daily price baseline."""
        now = (captured_at or datetime.now().astimezone()).astimezone()
        rows = self.universe_loader()
        new_high, new_low, price_details = store.update_market_daily_prices_and_high_low(
            rows,
            as_of=now.date().isoformat(),
            captured_at=now.isoformat(),
        )
        turnover, turnover_details = self.turnover_vs_ma20(captured_at=now)
        previous_inputs = []
        for record in store.list_market_sentiment_snapshots(limit=8):
            payload = (record.get("snapshot") or {}).get("input")
            if payload:
                previous_inputs.append(SentimentScoreInput.model_validate(payload))
        snapshot = self.capture(
            hs300_breadth_pct=hs300_breadth_pct,
            captured_at=now,
            historical_turnover_vs_ma20=turnover,
            new_high_count=new_high,
            new_low_count=new_low,
            previous_inputs=previous_inputs,
            broad_index_positive=turnover_details.get("broad_index_positive"),
            market_rows=rows,
        )
        return snapshot.model_copy(update={
            "source_details": {
                **snapshot.source_details,
                "market_price_history": price_details,
                "turnover_history": turnover_details,
            }
        }).with_source_hash()

    @staticmethod
    def turnover_vs_ma20(
        *, captured_at: datetime | None = None
    ) -> tuple[float | None, dict[str, Any]]:
        """Current two-market turnover versus the previous 20 closed sessions."""
        from pa_agent.data.eastmoney_client import fetch_index_daily

        now = (captured_at or datetime.now().astimezone()).astimezone()
        start = (now.date() - timedelta(days=60)).strftime("%Y%m%d")
        end = now.strftime("%Y%m%d")
        series: dict[date, float] = {}
        latest_direction: dict[str, bool] = {}
        for code in ("000001", "399001"):
            rows = fetch_index_daily(code, start_date=start, end_date=end)
            if len(rows) >= 2:
                latest_direction[code] = float(rows[-1]["close"]) >= float(rows[-2]["close"])
            for row in rows:
                value = row.get("time")
                day = value.date() if isinstance(value, datetime) else date.fromisoformat(str(value)[:10])
                series[day] = series.get(day, 0.0) + float(row.get("amount") or 0)
        ordered = sorted(series.items())
        if len(ordered) < 21:
            return None, {"reason": "requires_21_turnover_sessions", "sessions": len(ordered)}
        latest_day, latest_amount = ordered[-1]
        previous = [amount for _, amount in ordered[-21:-1]]
        baseline = sum(previous) / len(previous)
        return (
            latest_amount / baseline if baseline > 0 else None,
            {
                "latest_day": latest_day.isoformat(),
                "latest_turnover": latest_amount,
                "previous_20_mean": baseline,
                "sessions": len(ordered),
                "index_direction": latest_direction,
                "broad_index_positive": any(latest_direction.values()),
            },
        )


def fetch_limit_pool_snapshot(*, date: str) -> dict[str, Any]:
    """Read East Money's structured limit-up/down/blast pools."""
    import requests

    params = {
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "dpt": "wz.ztzt",
        "Pageindex": "0",
        "pagesize": "500",
        "sort": "fbt:asc",
        "date": date,
    }
    result: dict[str, Any] = {}
    source_as_of = ""
    for name, key in (
        ("getTopicZTPool", "limit_up"),
        ("getTopicDTPool", "limit_down"),
        ("getTopicZBPool", "blast"),
    ):
        response = requests.get(
            f"https://push2ex.eastmoney.com/{name}", params=params, timeout=15
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("rc") != 0 or not isinstance(payload.get("data"), dict):
            raise ValueError(f"eastmoney_{key}_pool_invalid")
        data = payload["data"]
        rows = list(data.get("pool") or [])
        result[key] = rows
        result[f"{key}_count"] = int(data.get("tc") or len(rows))
        qdate = str(data.get("qdate") or "")
        if qdate:
            source_as_of = max(source_as_of, qdate)
    result["source_as_of"] = source_as_of
    return result
