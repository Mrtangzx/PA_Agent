"""Risk/reward and estimated win-rate helpers for trading decisions."""
from __future__ import annotations

from typing import Any


def is_long_direction(direction: object) -> bool | None:
    """Return True for long, False for short, None if unknown."""
    text = str(direction or "").strip().lower()
    if not text:
        return None
    if "多" in text or text in ("long", "buy", "bull"):
        return True
    if "空" in text or text in ("short", "sell", "bear"):
        return False
    return None


def compute_risk_reward(
    entry: object,
    take_profit: object,
    stop_loss: object,
    direction: object,
) -> dict[str, float | str] | None:
    """Compute risk/reward distances and reward:risk ratio (盈亏比).

    Returns None when prices are invalid or risk is zero.
    """
    try:
        e = float(entry)
        tp = float(take_profit)
        sl = float(stop_loss)
    except (TypeError, ValueError):
        return None

    long = is_long_direction(direction)
    if long is True:
        risk = e - sl
        reward = tp - e
    elif long is False:
        risk = sl - e
        reward = e - tp
    else:
        if tp > e and sl < e:
            risk = e - sl
            reward = tp - e
        elif tp < e and sl > e:
            risk = sl - e
            reward = e - tp
        else:
            return None

    if risk <= 0 or reward <= 0:
        return None

    ratio = reward / risk
    return {
        "risk": risk,
        "reward": reward,
        "ratio": ratio,
        "ratio_text": f"{ratio:.2f} : 1",
    }


def format_estimated_win_rate(decision: dict[str, Any]) -> str | None:
    """Format model-provided estimated_win_rate (0–100) for display."""
    value = decision.get("estimated_win_rate")
    if value is None or value == "":
        return None
    try:
        pct = max(0, min(100, int(float(str(value).strip()))))
    except (ValueError, TypeError):
        return None
    return f"{pct}%"


def format_estimated_win_rate_reasoning(decision: dict[str, Any]) -> str:
    return str(decision.get("estimated_win_rate_reasoning", "") or "").strip()


# Lower cap: reward must be at least equal to risk (1:1) for any stance.
MIN_RISK_REWARD_RATIO = 1.0
# Kept only as a source-compatibility symbol. There is no active RR upper cap.
MAX_TP1_RISK_REWARD_RATIO = float("inf")


def min_risk_reward_ratio(decision_stance: str | None = None) -> float:
    """Minimum reward:risk ratio required to place an order (same for all stances)."""
    _ = decision_stance  # kept for call-site compatibility
    return MIN_RISK_REWARD_RATIO


def max_risk_reward_ratio() -> float | None:
    """There is intentionally no maximum reward:risk ratio."""
    return None


def widen_stop_for_tp1_rr_cap(
    entry: float,
    take_profit: float,
    stop_loss: float,
    direction: object,
    *,
    tick: float | None = None,
) -> float | None:
    """Compatibility API: validate geometry and return the unchanged structural stop."""
    _ = tick
    return stop_loss if compute_risk_reward(entry, take_profit, stop_loss, direction) else None


def adjust_decision_stop_for_tp1_rr_cap(
    decision: dict[str, Any],
    *,
    kline_frame: Any = None,
    tick: float | None = None,
) -> bool:
    """Deprecated compatibility shim which never mutates *decision*."""
    _ = decision, kline_frame, tick
    return False


def passes_trader_equation(
    win_rate_pct: float,
    risk: float,
    reward: float,
) -> bool:
    """Brooks equation: win_rate × reward > (1 - win_rate) × risk."""
    if risk <= 0 or reward <= 0:
        return False
    p = max(0.0, min(100.0, float(win_rate_pct))) / 100.0
    return p * reward > (1.0 - p) * risk


def compute_expectancy(
    win_rate_pct: float,
    risk: float,
    reward: float,
    *,
    round_trip_cost: float | None = None,
) -> dict[str, float | None | str]:
    """Return gross and optional net expectancy per unit.

    The win rate remains a model-provided, uncalibrated subjective estimate.
    A missing cost intentionally yields ``net_expectancy=None``.
    """
    if risk <= 0 or reward <= 0:
        raise ValueError("risk and reward must be positive")
    p = max(0.0, min(100.0, float(win_rate_pct))) / 100.0
    gross = p * reward - (1.0 - p) * risk
    return {
        "gross_expectancy": gross,
        "net_expectancy": gross - float(round_trip_cost) if round_trip_cost is not None else None,
        "win_rate_label": "未校准主观估计",
    }


def _parse_win_rate(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return max(0.0, min(100.0, float(str(value).strip())))
    except (TypeError, ValueError):
        return None


def _latest_closed_bar(kline_frame: Any) -> Any | None:
    """Return K1 (newest closed bar) from a snapshot frame."""
    bars = getattr(kline_frame, "bars", None) if kline_frame is not None else None
    if not bars:
        return None
    for bar in bars:
        if int(getattr(bar, "seq", 0) or 0) == 1 and bool(getattr(bar, "closed", True)):
            return bar
    for bar in bars:
        if bool(getattr(bar, "closed", True)):
            return bar
    return None


def validate_limit_order_k1_freshness(
    decision: dict[str, Any],
    kline_frame: Any,
    *,
    bar_analysis: dict[str, Any] | None = None,
) -> list[str]:
    """Reject stale limit orders that K1 has already traded through."""
    if decision.get("order_type") != "限价单":
        return []

    try:
        entry = float(decision.get("entry_price"))
        sl = float(decision.get("stop_loss_price"))
    except (TypeError, ValueError):
        return []

    bar = _latest_closed_bar(kline_frame)
    if bar is None:
        return []

    from pa_agent.util.price_tick import infer_price_tick_from_frame

    tick = infer_price_tick_from_frame(kline_frame) or 0.0
    k_high = float(bar.high)
    k_low = float(bar.low)
    k_close = float(bar.close)
    long = is_long_direction(decision.get("order_direction"))

    pending_planned = False
    if isinstance(bar_analysis, dict):
        entry_bar = bar_analysis.get("entry_bar")
        if isinstance(entry_bar, dict):
            freshness = str(entry_bar.get("freshness", "") or "").strip().lower()
            strength = str(entry_bar.get("strength", "") or "").strip().lower()
            pending_planned = (
                freshness == "pending"
                or strength == "not_triggered"
                or entry_bar.get("bar") is None
            )

    errors: list[str] = []
    if long is True:
        if pending_planned:
            # Planned buy limit: entry must stay below market close (waiting for dip).
            if k_close < entry - tick:
                errors.append(
                    f"limit long (planned): K1 close {k_close:.6g} is below entry {entry:.6g}; "
                    "reprice entry or 不下单"
                )
        else:
            if k_low <= entry + tick:
                errors.append(
                    f"limit long: K1 low {k_low:.6g} already touched/below entry {entry:.6g}; "
                    "pending buy limit is stale — use 市价单, reprice, or 不下单"
                )
            if k_close < entry - tick:
                errors.append(
                    f"limit long: K1 close {k_close:.6g} is below entry {entry:.6g}; "
                    "do not keep a buy limit above market without repricing"
                )
        if k_low <= sl + tick:
            errors.append(
                f"limit long: K1 low {k_low:.6g} already at/below stop {sl:.6g}; "
                "plan invalid — order_type=不下单"
            )
    elif long is False:
        if pending_planned:
            if k_close > entry + tick:
                errors.append(
                    f"limit short (planned): K1 close {k_close:.6g} is above entry {entry:.6g}; "
                    "reprice entry or 不下单"
                )
        else:
            if k_high >= entry - tick:
                errors.append(
                    f"limit short: K1 high {k_high:.6g} already reached/exceeded entry {entry:.6g}; "
                    "pending sell limit is stale — use 市价单, reprice, or 不下单"
                )
            if k_close > entry + tick:
                errors.append(
                    f"limit short: K1 close {k_close:.6g} is above entry {entry:.6g}; "
                    "do not keep a sell limit below market without repricing"
                )
        if k_high >= sl - tick:
            errors.append(
                f"limit short: K1 high {k_high:.6g} already at/above stop {sl:.6g}; "
                "plan invalid — order_type=不下单"
            )

    return errors


def validate_take_profit_2_geometry(
    decision: dict[str, Any],
) -> list[str]:
    """Ensure TP2 is beyond TP1 in the profit direction (no RR cap on TP2)."""
    entry = decision.get("entry_price")
    tp1 = decision.get("take_profit_price")
    tp2 = decision.get("take_profit_price_2")
    sl = decision.get("stop_loss_price")
    direction = decision.get("order_direction")

    try:
        e = float(entry)
        t1 = float(tp1)
        t2 = float(tp2)
        s = float(sl)
    except (TypeError, ValueError):
        return ["decision.take_profit_price_2: required finite number when placing an order"]

    long = is_long_direction(direction)
    if long is True:
        if not (s < e < t1 < t2):
            return [
                "decision.take_profit_price_2: long plan requires "
                "stop < entry < take_profit_price < take_profit_price_2"
            ]
    elif long is False:
        if not (t2 < t1 < e < s):
            return [
                "decision.take_profit_price_2: short plan requires "
                "take_profit_price_2 < take_profit_price < entry < stop"
            ]
    else:
        if t1 > e and t2 <= t1:
            return [
                "decision.take_profit_price_2: must be above take_profit_price for long geometry"
            ]
        if t1 < e and t2 >= t1:
            return [
                "decision.take_profit_price_2: must be below take_profit_price for short geometry"
            ]

    return []


def validate_order_trade_metrics(
    decision: dict[str, Any],
    *,
    decision_stance: str | None = None,
    kline_frame: Any = None,
    bar_analysis: dict[str, Any] | None = None,
    apply_rr_cap_adjustment: bool = False,
) -> list[str]:
    """Validate geometry and the RR floor without modifying input prices."""
    order_type = decision.get("order_type")
    if order_type not in ("限价单", "突破单", "市价单"):
        return []

    _ = apply_rr_cap_adjustment  # retained for source compatibility

    entry = decision.get("entry_price")
    tp = decision.get("take_profit_price")
    sl = decision.get("stop_loss_price")
    direction = decision.get("order_direction")
    rr = compute_risk_reward(entry, tp, sl, direction)
    if rr is None:
        return [
            "decision prices: entry/stop/target must form a valid long (sl<entry<tp) "
            "or short (tp<entry<sl) trade with positive risk and reward"
        ]

    errors: list[str] = []
    ratio = float(rr["ratio"])
    risk = float(rr["risk"])
    reward = float(rr["reward"])
    min_rr = min_risk_reward_ratio(decision_stance)

    if ratio < min_rr:
        errors.append(
            f"decision prices: risk_reward {rr['ratio_text']} is below minimum "
            f"{min_rr:.2f}:1 for this stance; adjust take_profit/stop_loss or set "
            "order_type=不下单 with 10.3=否"
        )

    max_rr = max_risk_reward_ratio()
    if max_rr is not None and ratio > max_rr + 1e-9:
        errors.append(
            f"decision prices: risk_reward {rr['ratio_text']} exceeds maximum "
            f"{max_rr:.2f}:1 for TP1 after stop adjustment; set order_type=不下单 "
            "with 10.3=否"
        )

    win_rate = _parse_win_rate(decision.get("estimated_win_rate"))
    if win_rate is None:
        errors.append(
            "decision.estimated_win_rate: required integer 0–100 when placing an order"
        )
    elif not passes_trader_equation(win_rate, risk, reward):
        ev = win_rate / 100.0 * reward - (1.0 - win_rate / 100.0) * risk
        errors.append(
            f"decision prices: trader equation fails at {win_rate:.0f}% win rate "
            f"(risk={risk:.4g}, reward={reward:.4g}, expectancy≈{ev:.4g}); "
            "10.3 must be 否 and order_type=不下单 unless prices are fixed"
        )

    if kline_frame is not None:
        errors.extend(
            validate_limit_order_k1_freshness(
                decision, kline_frame, bar_analysis=bar_analysis
            )
        )

    errors.extend(validate_take_profit_2_geometry(decision))

    return errors
