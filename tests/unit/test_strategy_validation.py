from pa_agent.trading.strategy_validation import run_fixed_mechanism_validation


def test_fixed_mechanism_suite_is_complete_deterministic_and_not_promotion_evidence() -> None:
    first = run_fixed_mechanism_validation()
    second = run_fixed_mechanism_validation()

    assert first.model_dump() == second.model_dump()
    assert first.status == "complete"
    assert not first.promotion_eligible
    assert len(first.checks) == 10
    assert all(check.passed for check in first.checks)
    assert first.input_hash == second.input_hash
    assert any("不证明策略收益" in item for item in first.limitations)
