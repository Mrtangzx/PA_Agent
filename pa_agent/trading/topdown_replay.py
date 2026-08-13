"""Deterministic, no-future-data replay for the 4:3:2:1 intraday gate."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from pydantic import BaseModel, Field

from pa_agent.trading.quant import SignalDecision, SignalStatus
from pa_agent.trading.topdown import (
    TopDownScoreSnapshot,
    TopDownScoreStatus,
    TopDownScoring,
    TopDownScoringContext,
)


class TopDownReplayFrame(BaseModel):
    """One frozen 15-minute decision point and its point-in-time pool evidence."""

    context: TopDownScoringContext
    pool_members: set[str]
    pool_effective_at: str
    pool_source_published_at: str


class TopDownReplayReport(BaseModel):
    strategy_version: str
    scoring_version: str
    status: str
    frame_count: int
    eligible_count: int
    first_eligible_at: str = ""
    input_hash: str
    scores: list[TopDownScoreSnapshot] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
    hard_failures: list[str] = Field(default_factory=list)
    input_hashes: list[str] = Field(default_factory=list)


class TopDownReplayEngine:
    """Replay frozen inputs in chronological order through the production scorer."""

    def __init__(self, scoring: TopDownScoring | None = None) -> None:
        self.scoring = scoring or TopDownScoring()

    def run(
        self,
        *,
        daily_signal: SignalDecision,
        frames: list[TopDownReplayFrame],
    ) -> TopDownReplayReport:
        gaps: list[str] = []
        failures: list[str] = []
        scores: list[TopDownScoreSnapshot] = []
        previous: TopDownScoreSnapshot | None = None
        previous_bar: datetime | None = None

        if daily_signal.status is not SignalStatus.ALLOW:
            failures.append("daily_signal_not_allowed")
        if not daily_signal.pool_version:
            gaps.append("daily_signal_pool_version_missing")
        if not daily_signal.signal_time:
            gaps.append("daily_signal_time_missing")

        for frame_number, frame in enumerate(frames, 1):
            context = frame.context
            prefix = f"frame_{frame_number}"
            bar_at = _parse_point(context.bar_closed_at, f"{prefix}_bar_time_invalid", gaps)
            if bar_at is None:
                continue
            if previous_bar is not None and bar_at <= previous_bar:
                failures.append(f"{prefix}_not_strictly_chronological")
            previous_bar = bar_at
            if context.symbol != daily_signal.symbol:
                failures.append(f"{prefix}_symbol_mismatch")
            if context.pool_version != daily_signal.pool_version:
                failures.append(f"{prefix}_pool_version_mismatch")
            if context.symbol not in frame.pool_members:
                failures.append(f"{prefix}_not_in_historical_pool")
            _require_not_after(
                frame.pool_effective_at, bar_at, f"{prefix}_pool_not_yet_effective", gaps, failures
            )
            _require_not_after(
                frame.pool_source_published_at,
                bar_at,
                f"{prefix}_pool_source_from_future",
                gaps,
                failures,
            )
            _require_not_after(
                daily_signal.signal_time,
                bar_at,
                f"{prefix}_daily_signal_from_future",
                gaps,
                failures,
            )
            for source, timestamp in context.required_source_timestamps.items():
                _require_not_after(
                    timestamp,
                    bar_at,
                    f"{prefix}_{source}_from_future",
                    gaps,
                    failures,
                )
            if failures or gaps:
                continue
            replay_context = context.model_copy(update={"previous_snapshot": previous})
            score = self.scoring.evaluate(replay_context)
            scores.append(score)
            previous = score

        data_gaps = list(dict.fromkeys([
            *gaps,
            *(gap for score in scores for gap in score.data_gaps),
        ]))
        hard_failures = list(dict.fromkeys(failures))
        eligible = [
            score for score in scores
            if score.status is TopDownScoreStatus.ELIGIBLE_FOR_RISK
        ]
        status = (
            "invalid" if hard_failures
            else "data_incomplete" if data_gaps
            else "complete"
        )
        return TopDownReplayReport(
            strategy_version=self.scoring.settings.strategy_version,
            scoring_version=self.scoring.settings.scoring_version,
            status=status,
            frame_count=len(frames),
            eligible_count=len(eligible),
            first_eligible_at=eligible[0].bar_closed_at if eligible else "",
            input_hash=_stable_hash({
                "daily_signal": daily_signal.model_dump(mode="json"),
                "frames": [frame.model_dump(mode="json") for frame in frames],
                "settings": self.scoring.settings.model_dump(mode="json"),
            }),
            scores=scores,
            data_gaps=data_gaps,
            hard_failures=hard_failures,
            input_hashes=[score.input_hash for score in scores],
        )


def _parse_point(value: str, error: str, gaps: list[str]) -> datetime | None:
    if not value:
        gaps.append(error)
        return None
    try:
        point = datetime.fromisoformat(value)
    except ValueError:
        gaps.append(error)
        return None
    if point.tzinfo is None:
        gaps.append(error)
        return None
    return point


def _require_not_after(
    value: str,
    bar_at: datetime,
    error: str,
    gaps: list[str],
    failures: list[str],
) -> None:
    point = _parse_point(value, f"{error}_timestamp_missing_or_invalid", gaps)
    if point is not None and point > bar_at:
        failures.append(error)


def _stable_hash(value: object) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
