import importlib.util
from pathlib import Path

from backend.keyword_research.budget import plan_budget
from backend.keyword_research.call_log import CallLog
from backend.keyword_research.pipeline import (
    _retry_budget_from_confirmed_plan,
    _used_retries_from_call_log,
)


def _validator_module():
    path = (Path(__file__).parents[1] / "backend" / "keyword_research" / "engine"
            / "scripts" / "validate_report.py")
    spec = importlib.util.spec_from_file_location("validate_report_budget_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_default_budget_limits_are_200_and_10():
    plan = plan_budget([{"marketplace": "US", "asins": ["A1"], "node_ids": []}])

    assert plan.hard_cap == 200
    assert plan.retry_budget == 10
    assert plan.as_dict()["hard_cap"] == 200
    assert plan.as_dict()["retry_budget"] == 10


def test_pipeline_reads_confirmed_retry_budget_and_legacy_default():
    assert _retry_budget_from_confirmed_plan({"confirmed": True,
                                               "budget": {"retry_budget": 10}}) == 10
    assert _retry_budget_from_confirmed_plan({"confirmed": True,
                                               "retry_budget": 7}) == 7
    assert _retry_budget_from_confirmed_plan({"confirmed": True, "planned_total": 32,
                                               "hard_cap": 35}) == 2


def test_pipeline_restores_retry_spend_from_existing_run_call_log(tmp_path):
    log = CallLog(tmp_path / "call_log.json")
    seq = log.start_call("tool", "retry previously started", is_retry=True)
    log.finish_call(seq, success=False, error="transport failure")
    seq = log.start_call("tool", "base request")
    log.finish_call(seq, success=False, error="transport failure")

    assert _used_retries_from_call_log(log.load()) == 1


def test_report_validator_uses_plan_retry_budget_and_legacy_default():
    validator = _validator_module()

    assert validator.retry_budget_from_plan({"budget": {"retry_budget": 10}}) == 10
    assert validator.retry_budget_from_plan({"retry_budget": 4}) == 4
    assert validator.retry_budget_from_plan({"planned_total": 32, "hard_cap": 35}) == 2
