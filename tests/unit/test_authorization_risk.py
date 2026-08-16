from __future__ import annotations

from pa_agent.trading.authorization_risk import (
    apply_topdown_authorization_revocation,
)
from pa_agent.trading.broker_models import AuthorizedOrder, PrefillClearReceipt
from pa_agent.trading.models import AssetClass, PlanStatus, TradePlan
from pa_agent.trading.store import TradeStore
from pa_agent.trading.topdown import TopDownScoreSnapshot, TopDownScoreStatus

NOW = "2026-08-14T10:15:00+08:00"


class _Broker:
    def __init__(self) -> None:
        self.calls = 0

    def clear_prefill_if_matches(self, order: AuthorizedOrder) -> PrefillClearReceipt:
        self.calls += 1
        return PrefillClearReceipt(
            status="cleared", created_at=NOW,
            verified_fields=order.model_dump(mode="json"),
        )


def _plan(store: TradeStore, plan_id: str, status: PlanStatus) -> None:
    decision = store.add_decision(
        symbol="600519", timeframe="15m", asset_class="a_share",
        original_decision={}, final_decision={}, meta={},
    )
    store.add_plan(TradePlan(
        id=plan_id, decision_event_id=decision, symbol="600519",
        timeframe="15m", asset_class=AssetClass.A_SHARE, direction="buy",
        entry_price=100, stop_loss_price=95, take_profit_price=110,
        order_type="limit",
        status=status,
    ))


def _score() -> TopDownScoreSnapshot:
    return TopDownScoreSnapshot(
        strategy_version="cloud_ai_topdown_4321_intraday_v1",
        scoring_version="1.0.0", symbol="600519", pool_version="cloud-ai",
        bar_closed_at=NOW, total_score=60, hard_blocks=["weak_market"],
        input_hash="score-hash", status=TopDownScoreStatus.AUTHORIZATION_REVOKED,
    )


def test_revocation_clears_matching_prefill_and_invalidates_unexecuted_plan(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    _plan(store, "prefilled", PlanStatus.AWAITING_USER_CONFIRMATION)
    order = AuthorizedOrder(
        plan_id="prefilled", account_fingerprint="account", symbol="600519",
        name="贵州茅台", direction="buy", price=100, quantity=100,
        stop_loss_price=95, strategy_id="s", authorized_at=NOW, expires_at=NOW,
    )
    store.append_event(
        "prefilled", "awaiting_user_confirmation",
        details={"authorized_order": order.model_dump(mode="json")},
    )
    broker = _Broker()

    actions = apply_topdown_authorization_revocation(
        store=store, score=_score(), broker_adapter=broker
    )

    assert store.get_plan("prefilled")["status"] == "invalidated"
    assert broker.calls == 1
    assert actions[0]["prefill_clear"]["status"] == "cleared"


def test_revocation_preserves_submitted_and_partial_fill_broker_truth(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    _plan(store, "submitted", PlanStatus.SUBMITTED)
    _plan(store, "partial", PlanStatus.PARTIALLY_FILLED)
    _plan(store, "filled", PlanStatus.FILLED)

    first = apply_topdown_authorization_revocation(store=store, score=_score())
    second = apply_topdown_authorization_revocation(store=store, score=_score())

    assert store.get_plan("submitted")["status"] == "submitted"
    assert store.get_plan("partial")["status"] == "partially_filled"
    assert store.get_plan("filled")["status"] == "filled"
    assert {item["event_type"] for item in first} == {
        "topdown_revocation_action_required"
    }
    assert second == []
