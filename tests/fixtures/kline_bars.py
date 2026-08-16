"""Synthetic newest-first K-line lists for tests."""
from __future__ import annotations

from pa_agent.data.base import KlineBar


def make_newest_first_bars(
    n: int,
    *,
    base_ts: float = 1_700_000_000.0,
    step_sec: float = 900.0,
    with_forming: bool = True,
    bullish_step: float = 0.0,
) -> list[KlineBar]:
    """Build closed bars plus an optional forming bar in newest-first order.

    ``bullish_step`` keeps the newest closed bar at the base price while
    lowering each older bar by the requested amount.  The default preserves
    the original flat fixture.
    """
    bars: list[KlineBar] = []
    if with_forming:
        bars.append(
            KlineBar(
                seq=1,
                ts_open=base_ts,
                open=2000.0 + bullish_step,
                high=2010.0 + bullish_step,
                low=1990.0 + bullish_step,
                close=2005.0 + bullish_step,
                volume=100.0,
                closed=False,
            )
        )
    start = 2 if with_forming else 1
    for seq in range(start, start + n):
        offset = -(seq - start) * bullish_step
        bars.append(
            KlineBar(
                seq=seq,
                ts_open=base_ts - (seq - 1) * step_sec,
                open=2000.0 + offset,
                high=2010.0 + offset,
                low=1990.0 + offset,
                close=2005.0 + offset,
                volume=100.0,
                closed=True,
            )
        )
    return bars
