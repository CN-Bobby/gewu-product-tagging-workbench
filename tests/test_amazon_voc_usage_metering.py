from __future__ import annotations

import sqlite3
import importlib.util
from pathlib import Path


RUNTIME = (Path(__file__).resolve().parents[1] / "backend" / "amazon_voc" /
           "runtime" / "voc_app")


def _load_runtime_module(name):
    spec = importlib.util.spec_from_file_location("amazon_voc_usage_test_" + name, RUNTIME / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


Database = _load_runtime_module("db").Database
provider_module = _load_runtime_module("provider")
_usage_tokens = provider_module._usage_tokens


def test_chat_usage_reads_deepseek_cache_hit_and_miss_tokens():
    usage = _usage_tokens({
        "prompt_tokens": 120,
        "completion_tokens": 15,
        "prompt_cache_hit_tokens": 80,
        "prompt_cache_miss_tokens": 40,
    }, "chat")

    assert usage == {
        "input_tokens": 120,
        "output_tokens": 15,
        "cache_hit_input_tokens": 80,
        "cache_miss_input_tokens": 40,
    }


def test_chat_usage_reads_standard_cached_tokens_without_inventing_miss_count():
    usage = _usage_tokens({
        "prompt_tokens": 120,
        "completion_tokens": 15,
        "prompt_tokens_details": {"cached_tokens": 80},
    }, "chat")

    assert usage["cache_hit_input_tokens"] == 80
    assert usage["cache_miss_input_tokens"] is None


def test_responses_usage_reads_cached_input_tokens():
    usage = _usage_tokens({
        "input_tokens": 120,
        "output_tokens": 15,
        "input_tokens_details": {"cached_tokens": 80},
    }, "responses")

    assert usage["input_tokens"] == 120
    assert usage["output_tokens"] == 15
    assert usage["cache_hit_input_tokens"] == 80
    assert usage["cache_miss_input_tokens"] is None


def test_missing_usage_and_cache_fields_remain_unknown():
    usage = _usage_tokens({}, "chat")

    assert usage == {
        "input_tokens": None,
        "output_tokens": None,
        "cache_hit_input_tokens": None,
        "cache_miss_input_tokens": None,
    }


def test_provider_returns_reported_cache_usage_without_network_calls():
    class Settings:
        model_api_key = "test-only"
        model_base_url = "https://model.invalid/v1"
        model_protocol = "chat"

    provider = provider_module.OpenAICompatibleProvider(Settings())
    provider._request = lambda _url, _body, **_kwargs: {
        "choices": [{"message": {"content": '{"ok":true}'}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3,
                  "prompt_cache_hit_tokens": 8, "prompt_cache_miss_tokens": 4},
    }

    result, usage = provider.call("unit", "deepseek-chat", "instructions", {}, {})

    assert result == {"ok": True}
    assert usage["input_tokens"] == 12
    assert usage["output_tokens"] == 3
    assert usage["cache_hit_input_tokens"] == 8
    assert usage["cache_miss_input_tokens"] == 4


def test_provider_parses_json_inside_markdown_wrapper():
    class Settings:
        model_api_key = "test-only"
        model_base_url = "https://model.invalid/v1"
        model_protocol = "chat"

    provider = provider_module.OpenAICompatibleProvider(Settings())
    provider._request = lambda *_args, **_kwargs: {
        "choices": [{"message": {"content": 'JSON result:\n```json\\n{"ok":true}\\n```'},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
    }

    result, usage = provider.call("unit", "deepseek-chat", "instructions", {}, {})

    assert result == {"ok": True}
    assert usage["input_tokens"] == 12
    assert usage["output_tokens"] == 3


def test_invalid_json_error_retains_token_usage_and_response_metadata():
    class Settings:
        model_api_key = "test-only"
        model_base_url = "https://model.invalid/v1"
        model_protocol = "chat"

    provider = provider_module.OpenAICompatibleProvider(Settings())
    provider._request = lambda *_args, **_kwargs: {
        "choices": [{"message": {"content": '{"incomplete":'}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 99},
    }

    try:
        provider.call("unit", "deepseek-chat", "instructions", {}, {})
    except provider_module.ModelError as error:
        assert error.usage["input_tokens"] == 100
        assert error.usage["output_tokens"] == 99
        assert error.response_meta == {"finish_reason": "length", "output_chars": 14}
        assert "finish_reason=length" in str(error)
    else:
        raise AssertionError("invalid JSON response should be rejected")


def test_full_voc_v2_uses_configured_long_timeout_without_network_calls():
    class Settings:
        model_api_key = "test-only"
        model_base_url = "https://model.invalid/v1"
        model_protocol = "chat"
        full_voc_timeout_seconds = 777

    provider = provider_module.OpenAICompatibleProvider(Settings())
    observed = []

    def fake_request(_url, _body, **kwargs):
        observed.append((kwargs.get("timeout"), _body))
        return {"choices": [{"message": {"content": '{"ok":true}'}}], "usage": {}}

    provider._request = fake_request
    provider.call("full_voc_v2", "deepseek-v4-flash", "instructions", {}, {})

    assert observed[0][0] == 777
    assert observed[0][1]["max_tokens"] == provider_module.DEEPSEEK_V4_MAX_OUTPUT_TOKENS


def test_full_voc_v2_responses_sets_deepseek_output_cap_without_network_calls():
    class Settings:
        model_api_key = "test-only"
        model_base_url = "https://model.invalid/v1"
        model_protocol = "responses"
        full_voc_timeout_seconds = 777

    provider = provider_module.OpenAICompatibleProvider(Settings())
    observed = []

    def fake_request(_url, body, **kwargs):
        observed.append((kwargs.get("timeout"), body))
        return {"output_text": '{"ok":true}', "usage": {}}

    provider._request = fake_request
    provider.call("full_voc_v2", "deepseek-v4-flash", "instructions", {}, {})

    assert observed[0][0] == 777
    assert observed[0][1]["max_output_tokens"] == provider_module.DEEPSEEK_V4_MAX_OUTPUT_TOKENS


def test_model_call_recording_accepts_old_and_new_optional_arguments(tmp_path):
    db = Database(tmp_path / "voc.sqlite3")
    project = db.create_project("Usage", "US", "US", "en", "Tops")
    run = db.create_run(project["id"], {})

    legacy_call_id = db.record_model_call(
        run["id"], 1, "extract_events", "b1", "hash1", "fake", "v1", "chat",
        10, 100, 20, 0, "completed",
    )
    new_call_id = db.record_model_call(
        run["id"], 1, "extract_events", "b2", "hash2", "fake", "v1", "chat",
        10, 100, 20, 0, "completed", cache_hit_input_tokens=70,
        cache_miss_input_tokens=30,
    )

    legacy = db.one("SELECT * FROM model_calls WHERE id=?", (legacy_call_id,))
    current = db.one("SELECT * FROM model_calls WHERE id=?", (new_call_id,))
    assert legacy["cache_hit_input_tokens"] is None
    assert legacy["cache_miss_input_tokens"] is None
    assert current["cache_hit_input_tokens"] == 70
    assert current["cache_miss_input_tokens"] == 30

    assert db.update_model_call(new_call_id, status="completed") == 1
    current = db.one("SELECT * FROM model_calls WHERE id=?", (new_call_id,))
    assert current["cache_hit_input_tokens"] == 70
    assert current["cache_miss_input_tokens"] == 30
    assert db.update_model_call(new_call_id, cache_miss_input_tokens=None) == 1
    assert db.one("SELECT cache_miss_input_tokens FROM model_calls WHERE id=?", (new_call_id,))["cache_miss_input_tokens"] is None


def test_existing_database_gets_idempotent_usage_and_workspace_columns(tmp_path):
    path = tmp_path / "legacy-voc.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE projects (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, marketplace TEXT NOT NULL,
            country TEXT NOT NULL, language TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE model_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, revision INTEGER NOT NULL,
            stage TEXT NOT NULL, batch_id TEXT NOT NULL, input_hash TEXT NOT NULL, model TEXT NOT NULL,
            prompt_version TEXT NOT NULL, protocol TEXT NOT NULL, latency_ms INTEGER NOT NULL,
            input_tokens INTEGER, output_tokens INTEGER, retries INTEGER NOT NULL, status TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        )""")

    Database(path)
    Database(path)
    with sqlite3.connect(path) as conn:
        project_columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
        call_columns = [row[1] for row in conn.execute("PRAGMA table_info(model_calls)")]

    assert "workspace_id" in project_columns
    assert call_columns.count("cache_hit_input_tokens") == 1
    assert call_columns.count("cache_miss_input_tokens") == 1

    db = Database(path)
    project = db.create_project("Scoped", "US", "US", "en", "Tops", workspace_id="workspace-1")
    run = db.create_run(project["id"], {})
    assert db.get_run(run["id"])["workspace_id"] == "workspace-1"
