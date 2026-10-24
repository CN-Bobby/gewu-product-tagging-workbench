import json
import threading
from datetime import datetime

import pytest
from sqlalchemy import select

from backend import batch, usage
from backend.amazon_reviews import sorftime
from backend.db import Session, UsageEvent, UsageTaskLink, Workspace, uid


def _stored_events():
    with Session() as session:
        return list(session.scalars(select(UsageEvent).order_by(UsageEvent.occurred_at.asc())))


def test_normalize_deepseek_and_qwen_cached_and_uncached_input():
    deepseek = usage.normalize_provider_usage({
        "prompt_tokens": 100,
        "completion_tokens": 12,
        "prompt_cache_hit_tokens": 28,
    })
    assert deepseek == {
        "input_tokens": 100, "output_tokens": 12,
        "cache_hit_tokens": 28, "cache_miss_tokens": 72,
    }

    qwen = usage.normalize_provider_usage({
        "input_tokens": 120,
        "output_tokens": 9,
        "input_tokens_details": {"cached_tokens": 45, "uncached_tokens": 75},
    })
    assert qwen == {
        "input_tokens": 120, "output_tokens": 9,
        "cache_hit_tokens": 45, "cache_miss_tokens": 75,
    }

    assert usage.normalize_provider_usage({"prompt_tokens": 12}) == {"input_tokens": 12}


def test_batch_model_request_records_measured_tokens_and_vision_attribution(monkeypatch):
    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
                "usage": {
                    "input_tokens": 120, "output_tokens": 9,
                    "input_tokens_details": {"cached_tokens": 45, "uncached_tokens": 75},
                },
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def post(*_args, **_kwargs):
            return Response()

    monkeypatch.setattr(batch, "provider_config", lambda *_args: (
        "https://api.qwen.example/v1", "synthetic-key", "qwen-text"))
    monkeypatch.setattr(batch.httpx, "Client", Client)
    metrics = {}
    result = batch._call_with_usage_module(
        "vision", "qwen/qwen-text", [{"role": "user", "content": "image question"}],
        metrics, threading.Lock())

    assert result == {"ok": True}
    assert metrics["input_tokens"] == 120 and metrics["output_tokens"] == 9
    [event] = _stored_events()
    assert (event.kind, event.module, event.provider, event.model, event.status) == (
        "model", "vision", "qwen", "qwen/qwen-text", "success")
    assert (event.input_tokens, event.output_tokens) == (120, 9)
    assert (event.cache_hit_tokens, event.cache_miss_tokens) == (45, 75)
    assert event.usage_quality == "measured"
    assert "synthetic-key" not in json.dumps(event.metadata_json)


def test_missing_provider_usage_stays_estimated_and_local_cache_is_separate():
    event_id = usage.start_usage_event(
        kind="model", module="keyword_research", task_id="kw-task",
        task_title="用量测试关键词", model="deepseek/chat", provider="deepseek",
        request_text="搜索家居照明关键词")
    usage.finish_usage_event(event_id, status="success", usage={},
                             request_text="搜索家居照明关键词", response_text="关键词报告")
    usage.record_local_cache(module="keyword_research", task_id="kw-task",
                             cache_name="keyword_result", hit=True)

    rows = {event.kind: event for event in _stored_events()}
    model_event = rows["model"]
    assert model_event.input_tokens is None and model_event.output_tokens is None
    assert model_event.estimated_input_tokens and model_event.estimated_output_tokens
    assert model_event.usage_quality == "estimated"
    cache_event = rows["local_cache"]
    assert cache_event.status == "hit" and cache_event.input_tokens is None


def test_agent_turn_records_cli_aggregate_usage_without_inventing_model_call_count():
    event_id = usage.start_usage_event(
        kind="agent_turn", module="product_archive", task_id="archive-run",
        model="deepseek/reasoning", provider="deepseek")
    usage.finish_usage_event(event_id, status="completed", usage={
        "input_tokens": 195052, "output_tokens": 14133,
        "cache_read_input_tokens": 164736,
        "cache_creation_input_tokens": 30316,
    })

    [event] = _stored_events()
    assert event.kind == "agent_turn"
    assert event.usage_quality == "measured"
    assert (event.input_tokens, event.output_tokens) == (195052, 14133)
    assert event.cache_hit_tokens == 164736
    assert event.cache_creation_tokens == 30316
    totals = usage.summary({"range": "today"})["totals"]
    assert totals["agent_turns"] == 1
    assert totals["model_calls"] == 0
    assert totals["input_tokens"] == 195052
    assert totals["output_tokens"] == 14133


def test_saved_reasonix_envelope_backfills_failed_event_without_new_call():
    from backend.product_archive import reasonix_worker

    event_id = usage.start_usage_event(
        kind="agent_turn", module="product_archive", task_id="saved-run",
        model="deepseek/reasoning", provider="deepseek")
    usage.finish_usage_event(event_id, status="failed")

    reasonix_worker.reconcile_recorded_usage("saved-run", {
        "usage": {"input_tokens": 400, "output_tokens": 50,
                  "cache_read_input_tokens": 300}})

    [event] = _stored_events()
    assert event.id == event_id and event.status == "failed"
    assert event.usage_quality == "measured"
    assert (event.input_tokens, event.output_tokens, event.cache_hit_tokens) == (400, 50, 300)


def test_sorftime_mcp_call_is_counted_without_storing_request_or_key(monkeypatch):
    async def fake_call(_endpoint, _key, _name, _arguments):
        return {"ok": True}

    monkeypatch.setattr(sorftime, "_call", fake_call)
    context = {"module": "amazon_reviews", "task_id": "review-task",
               "task_title": "评论测试", "attempt": 2, "review_round": 1}
    with sorftime.usage_scope(context):
        assert sorftime.call("https://mcp.example", "synthetic-secret", "product_reviews",
                             {"asin": "B000000000"}) == {"ok": True}

    [event] = _stored_events()
    assert (event.kind, event.module, event.task_id, event.server_name) == (
        "mcp", "amazon_reviews", "review-task", "Sorftime")
    assert (event.function_name, event.attempt, event.status) == ("product_reviews", 2, "success")
    assert event.metadata_json["review_round"] == 1
    serialized = json.dumps(usage.events({"range": "today"}), ensure_ascii=False)
    assert "synthetic-secret" not in serialized and "B000000000" not in serialized


def test_summary_groups_usage_by_project_task_and_time_range():
    with Session.begin() as session:
        workspace = Workspace(id=uid(), name="费用归集测试项目")
        session.add(workspace)

    model_id = usage.start_usage_event(
        kind="model", module="usage_test", task_id="task-1", task_title="演示任务",
        model="qwen/text", provider="qwen", request_text="input")
    usage.finish_usage_event(model_id, status="success", usage={
        "prompt_tokens": 100, "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 40},
    })
    mcp_id = usage.start_usage_event(
        kind="mcp", module="usage_test", task_id="task-1", function_name="search",
        server_name="Demo MCP")
    usage.finish_usage_event(mcp_id, status="success")
    usage.record_local_cache(module="usage_test", task_id="task-1", cache_name="result", hit=False)
    usage.set_task_project("usage_test", "task-1", workspace.id)

    result = usage.summary({"range": "today", "bucket": "day"})
    assert result["totals"]["model_calls"] == 1
    assert result["totals"]["mcp_calls"] == 1
    assert result["totals"]["input_tokens"] == 100
    assert result["totals"]["output_tokens"] == 20
    assert result["totals"]["cache_hit_tokens"] == 40
    assert result["totals"]["cache_miss_tokens"] == 60
    assert result["totals"]["local_result_cache_misses"] == 1
    project = next(row for row in result["by_project"] if row["workspace_id"] == workspace.id)
    assert project["workspace_name"] == "费用归集测试项目"
    assert project["model_calls"] == 1 and project["input_tokens"] == 100
    task = next(row for row in result["by_task"] if row["task_id"] == "task-1")
    assert task["workspace_id"] == workspace.id and task["mcp_calls"] == 1
    assert task["local_result_cache_misses"] == 1

    with Session() as session:
        [link] = list(session.scalars(select(UsageTaskLink).where(
            UsageTaskLink.module == "usage_test", UsageTaskLink.task_id == "task-1")))
    event_before = next(event for event in _stored_events() if event.id == model_id)
    occurred_at, token_count = event_before.occurred_at, event_before.input_tokens
    assert link.workspace_id == workspace.id

    local_today = datetime.now(usage.configured_timezone()).date()
    custom = usage.summary({"range": "custom", "start": local_today.isoformat(),
                            "end": local_today.isoformat(), "bucket": "day"})
    week = usage.summary({"range": "this_week", "bucket": "week"})
    month = usage.summary({"range": "this_month", "bucket": "month"})
    assert custom["range"]["mode"] == "custom" and len(custom["series"]) == 1
    assert week["range"]["mode"] == "this_week" and len(week["series"]) == 1
    assert month["range"]["mode"] == "this_month" and len(month["series"]) == 1
    with pytest.raises(ValueError, match="结束日期不能早于开始日期"):
        usage.summary({"range": "custom", "start": "2026-10-18", "end": "2026-10-17"})

    with Session() as session:
        event_after = session.get(UsageEvent, model_id)
        assert event_after.occurred_at == occurred_at and event_after.input_tokens == token_count
