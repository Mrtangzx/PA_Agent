from __future__ import annotations

from pa_agent.trading.models import RiskSettings
from pa_agent.trading.service import TradingService
from pa_agent.trading.store import TradeStore


def _decision(direction: str = "做多", confidence: int = 10) -> dict:
    return {
        "order_type": "限价单", "order_direction": direction,
        "entry_price": 100, "stop_loss_price": 95,
        "take_profit_price": 110, "take_profit_price_2": 115,
        "take_profit_basis": "区间对侧阻力", "take_profit_basis_2": "区间测量移动",
        "trade_confidence": confidence, "estimated_win_rate": 55,
        "reasoning": "区间边界方案",
    }


def test_ai_decision_is_audited_but_cannot_create_executable_plan(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    service = TradingService(store, RiskSettings())
    result = service.persist_stage2_decision(
        decision_inner=_decision(confidence=1), stage2_full={"diagnosis_summary": {}},
        symbol="600519", timeframe="1d", data_source="akshare", record_meta={},
    )
    assert result["decision_id"]
    assert result["plan_id"] is None
    assert result["execution_blocked_reason"] == "ai_research_only"
    assert result["risk_snapshot"]["quantity"] is None
    metrics = result["final_decision"]["program_trade_metrics"]
    assert metrics["gross_expectancy"] is not None
    assert metrics["net_expectancy"] is None


def test_a_share_short_is_converted_to_no_order_and_audited(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    service = TradingService(store)
    result = service.persist_stage2_decision(
        decision_inner=_decision(direction="做空"), stage2_full={"diagnosis_summary": {}},
        symbol="600519", timeframe="1d", data_source="akshare", record_meta={},
    )
    assert result["plan_id"] is None
    assert result["final_decision"]["order_type"] == "不下单"
    audit = store.get_decision(result["decision_id"])
    assert "禁止做空" in audit["audit_reason"]


def test_target_without_structure_basis_is_rejected_but_audited(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    service = TradingService(store)
    decision = _decision(); decision["take_profit_basis"] = ""; decision["take_profit_basis_2"] = ""; decision["reasoning"] = "test"
    result = service.persist_stage2_decision(
        decision_inner=decision, stage2_full={}, symbol="AU0", timeframe="1h",
        data_source="eastmoney_futures", record_meta={},
    )
    assert result["decision_id"]
    assert result["plan_id"] is None
    assert "缺少" in store.get_decision(result["decision_id"])["audit_reason"]
