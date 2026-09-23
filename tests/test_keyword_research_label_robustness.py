import json

import pytest

from backend.keyword_research.llm_client import LLMError, LLMResult
from backend.keyword_research.llm_label import LLMLabeler


def _valid_row(keyword):
    return {
        "keyword": keyword,
        "translation_fixed": f"中文-{keyword}",
        "translation_reason": "直译准确",
        "translation_confidence": "high",
        "family": "测试品类",
        "demand_tags": [],
        "relevance": "A",
        "relevance_reason": "品类相关",
        "purchase_intent": "高",
    }


def _keywords(count):
    return [{"keyword": f"keyword-{i}", "keywordCn": ""}
            for i in range(count)]


class _ShapeErrorLLM:
    def __init__(self, poison=None):
        self.calls = []
        self.poison = poison

    def complete(self, messages, *, json_mode=True):
        requested = json.loads(messages[1]["content"])["关键词列表"]
        keys = [row["keyword"] for row in requested]
        self.calls.append(keys)
        if self.poison is not None and self.poison in keys:
            return LLMResult(json.dumps([_valid_row(k) if k != self.poison else "bad-row"
                                         for k in keys]))
        if len(keys) == 10:
            # One non-object among otherwise schema-valid rows invalidates this response.
            return LLMResult(json.dumps([_valid_row(k) if i != 3 else "bad-row"
                                         for i, k in enumerate(keys)]))
        return LLMResult(json.dumps([_valid_row(k) for k in keys]))


class _DuplicateAndWrongKeyLLM:
    def __init__(self):
        self.calls = []

    def complete(self, messages, *, json_mode=True):
        keys = [row["keyword"] for row in
                json.loads(messages[1]["content"])["关键词列表"]]
        self.calls.append(keys)
        if len(self.calls) == 1:
            return LLMResult(json.dumps([
                _valid_row("keyword-0"),
                _valid_row("keyword-1"), _valid_row("keyword-1"),
                _valid_row("unexpected-keyword"),
            ]))
        return LLMResult(json.dumps([_valid_row(key) for key in keys]))


class _ExhaustedConnectionLLM:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, *, json_mode=True):
        self.calls += 1
        raise LLMError("连接失败（已尝试 4 次）", retryable=True,
                       fallback_allowed=False)


class _NonRetryableTimeoutLLM:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, *, json_mode=True):
        self.calls += 1
        raise LLMError("读取响应超时", retryable=False,
                       fallback_allowed=False)


def test_non_object_row_recovers_by_splitting_only_the_failed_batch(tmp_path):
    path = tmp_path / "keyword_labeled.json"
    path.write_text(json.dumps({"keywords": _keywords(10)}), encoding="utf-8")
    llm = _ShapeErrorLLM()
    events = []

    stats = LLMLabeler(llm, on_event=events.append).label_file(path, concurrency=1)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert llm.calls == [
        [f"keyword-{i}" for i in range(10)],
        ["keyword-3"],
    ]
    assert all(k.get("translation_fixed") == f"中文-{k['keyword']}"
               for k in saved["keywords"])
    assert stats["labeled"] == 10
    assert stats["split_batches"] == 0
    assert stats["failed_batches"] == 0
    assert stats["completed_batches"] == 1
    assert any(event["kind"] == "label_batch_done" for event in events)
    assert any(event["kind"] == "label_batch_partial"
               and event["unresolved"] == 1 for event in events)


def test_persistent_bad_singleton_keeps_good_sibling_and_fabricates_nothing(tmp_path):
    path = tmp_path / "keyword_labeled.json"
    path.write_text(json.dumps({"keywords": _keywords(2)}), encoding="utf-8")
    llm = _ShapeErrorLLM(poison="keyword-0")

    with pytest.raises(RuntimeError, match="标签批次失败"):
        LLMLabeler(llm).label_file(path, concurrency=1)

    saved = json.loads(path.read_text(encoding="utf-8"))
    by_key = {row["keyword"]: row for row in saved["keywords"]}
    assert by_key["keyword-1"]["translation_fixed"] == "中文-keyword-1"
    assert not by_key["keyword-0"].get("translation_fixed")
    assert "translation_fixed" not in by_key["keyword-0"]
    assert [len(call) for call in llm.calls] == [2, 1]
    assert saved["labeled_stats"]["labeled"] == 1
    assert saved["labeled_stats"]["failed_batches"] == 1
    assert saved["labeled_stats"]["retries"] == 1


def test_duplicate_and_wrong_key_rows_leave_only_unresolved_keywords_for_retry(tmp_path):
    path = tmp_path / "keyword_labeled.json"
    path.write_text(json.dumps({"keywords": _keywords(3)}), encoding="utf-8")
    llm = _DuplicateAndWrongKeyLLM()

    stats = LLMLabeler(llm).label_file(path, concurrency=1)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert llm.calls == [
        ["keyword-0", "keyword-1", "keyword-2"],
        ["keyword-1", "keyword-2"],
    ]
    assert all(row.get("translation_fixed") == f"中文-{row['keyword']}"
               for row in saved["keywords"])
    assert stats["labeled"] == 3


def test_exhausted_connection_failure_is_not_repeated_by_labeler(tmp_path):
    path = tmp_path / "keyword_labeled.json"
    path.write_text(json.dumps({"keywords": _keywords(2)}), encoding="utf-8")
    llm = _ExhaustedConnectionLLM()

    with pytest.raises(RuntimeError, match="标签批次失败"):
        LLMLabeler(llm).label_file(path, concurrency=1)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert llm.calls == 1
    assert saved["labeled_stats"]["labeled"] == 0
    assert saved["labeled_stats"]["failed_batches"] == 1
    assert saved["labeled_stats"]["retries"] == 0


def test_non_retryable_timeout_is_not_retried_and_creates_no_labels(tmp_path):
    path = tmp_path / "keyword_labeled.json"
    path.write_text(json.dumps({"keywords": _keywords(2)}), encoding="utf-8")
    llm = _NonRetryableTimeoutLLM()

    with pytest.raises(RuntimeError, match="标签批次失败"):
        LLMLabeler(llm).label_file(path, concurrency=1)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert llm.calls == 1
    assert saved["labeled_stats"]["labeled"] == 0
    assert all("translation_fixed" not in row for row in saved["keywords"])
    assert saved["labeled_stats"]["retries"] == 0
