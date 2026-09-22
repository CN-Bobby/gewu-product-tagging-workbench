# -*- coding: utf-8 -*-
"""关键词调研引擎离线端到端测试。

用注入的 FakeMCP（历史真实录制响应）+ FakeLLM（确定性应答）替换真实网络，
完整跑通 Pipeline.run() 并校验真的产出 13 Sheet 的 xlsx 报告。
"""
import importlib.util
import json
import re
import socket
import threading
from pathlib import Path

import openpyxl
import pytest

from backend.keyword_research.config import EngineConfig
from backend.keyword_research.llm_client import LLMResult
from backend.keyword_research.mcp_client import MCPClient
from backend.keyword_research.pipeline import Pipeline

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "keyword_research" / "mcp_responses"
ENGINE_SCRIPTS = Path(__file__).resolve().parents[1] / "backend" / "keyword_research" / "engine" / "scripts"

# 录制夹具只在本机存在，不入版本库：仓库审计禁止 ASIN 形状的字面量与模型响应原文入库。
# 缺夹具时明确 skip，而不是让离线端到端悄悄消失。
pytestmark = pytest.mark.skipif(
    not FIXTURES.is_dir() or not any(FIXTURES.glob("*.json")),
    reason="本机没有真实录制夹具（不入库），离线端到端跳过")

def _fixture_asins() -> list[str]:
    """覆盖面由夹具本身决定：ASIN 从文件名读出来，仓库里不写任何 ASIN 字面量。"""
    return sorted({path.name.split("_")[3]
                   for path in FIXTURES.glob("traffic_keyword_current_*_p1.json")})


# fixtures 完整覆盖的那批 US ASIN（历史任务录制）
HIST_ASINS = _fixture_asins()


def _engine_sheet_names() -> list[str]:
    """从引擎脚本读取权威 Sheet 清单（不复制常量）。"""
    spec = importlib.util.spec_from_file_location(
        "kw_build_excel_report", ENGINE_SCRIPTS / "build_excel_report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return list(module.SHEET_NAMES)


def _slug(keyword: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", keyword.lower()).strip("_")
    return s or "kw"


class FakeMCP(MCPClient):
    """复用 MCPClient 的限流/日志/重试逻辑，只把网络层换成录制响应。"""

    def __init__(self, fixtures_dir: Path, rate_limiter, call_log, on_event=None) -> None:
        super().__init__("fake://offline", rate_limiter, call_log, on_event)
        self.fixtures = Path(fixtures_dir)

    def _read(self, name: str) -> dict:
        path = self.fixtures / name
        if not path.exists():
            raise FileNotFoundError(f"fixture 缺失: {name}")
        return json.loads(path.read_text(encoding="utf-8"))

    async def _call_once(self, tool: str, arguments: dict) -> dict:
        if tool == "traffic_extend":
            page = arguments.get("request", {}).get("page", 1)
            return self._read(f"traffic_batch_1_p{page}.json")
        if tool == "traffic_keyword":
            asin = arguments.get("request", {}).get("asin", "")
            return self._read(f"traffic_keyword_current_{asin}_p1.json")
        if tool == "aba_research_trend":
            keyword = arguments.get("keyword", "")
            name = f"trend_{_slug(keyword)}.json"
            if (self.fixtures / name).exists():
                return self._read(name)
            return self._synthetic_trend(keyword)
        if tool == "product_node":
            return {"code": "OK",
                    "data": {"nodeIdPath": arguments.get("request", {}).get("nodeIdPath")}}
        if tool == "product_research":
            return {"code": "OK", "data": {"items": []}}
        raise NotImplementedError(f"FakeMCP 未实现工具: {tool}")

    @staticmethod
    def _synthetic_trend(keyword: str) -> dict:
        """24 个月确定性趋势（analyze_trends.py 可解析的格式）。"""
        import datetime as dt

        items = []
        base = 50000 + (hash(keyword) % 900000)
        end = dt.date(2026, 7, 1)
        for i in range(36):
            m = end.month - i - 1
            y = end.year
            while m < 0:
                m += 12
                y -= 1
            wave = 1 + 0.3 * ((m + 1) % 12 in (6, 7, 8))
            items.append({
                "date": int(dt.datetime(y, m + 1, 1).timestamp() * 1000),
                "rank": max(1, 500 - i * 8),
                "searches": int(base * wave * (1 + 0.05 * (i % 5))),
                "label": f"{y}{m + 1:02d}",
            })
        items.reverse()
        return {"code": "OK", "message": "成功", "data": items}


class FakeLLM:
    """确定性应答：标注返回规则标签，分析返回固定骨架；不发起任何请求。"""

    def __init__(self) -> None:
        self.detected_mode = "chat"
        self.calls: list[str] = []

    def _label_response(self, keywords: list[dict]) -> list[dict]:
        rows = []
        for k in keywords:
            kw = k["keyword"]
            low = kw.lower()
            tags = []
            if any(w in low for w in ("for kids", "for boys", "for girls", " kids", " boys", " girls")):
                tags.append("audience:kids")
            elif any(w in low for w in ("for women", "for men", " womens", " mens", " women", " men")):
                tags.append("audience:adult")
            if any(w in low for w in ("steel", "plastic", "wood", "glass", "silicone")):
                tags.append("material:mock")
            else:
                tags.append("function:general")
            rows.append({
                "keyword": kw,
                "translation_fixed": k.get("keywordCn") or kw,
                "translation_reason": "确定性 mock 翻译",
                "translation_confidence": "high",
                "family": (kw.split()[0] if kw.split() else kw)[:12],
                "demand_tags": tags,
                "relevance": "A" if (k.get("searches") or 0) >= 10000 else "B",
                "relevance_reason": "确定性 mock 相关度",
                "purchase_intent": "高" if (k.get("purchaseRate") or 0) >= 0.03 else "中",
                "is_brand": bool(k.get("is_brand_candidate")),
                "is_noise": False,
                "local_context": "",
            })
        return rows

    @staticmethod
    def _analysis_response(keywords: list[dict]) -> dict:
        sample = ["lunch box", "lunch bag", "meal prep containers"]
        return {
            "category_label": "mock品类",
            "demand_map": [{
                "tag": "core_category:mock",
                "kw_count": len(keywords),
                "representative": sample,
                "searches_median": 10000,
                "purchase_rate_median": 0.02,
                "click_conc_median": None,
                "conv_conc_median": None,
                "ppc_median": None,
                "trend_direction": "flat",
                "product_implication": "确定性 mock 需求聚合。",
            }],
            "traffic_traps": [],
            "quadrant": {
                "click_median": 0.1,
                "conv_median": 0.05,
                "items": [
                    {"keyword": "lunch box", "click": 0.1414, "conv": 0.1128,
                     "quadrant": "high-high", "interpretation": "mock 双高"},
                    {"keyword": "lunch bag", "click": 0.082, "conv": 0.041,
                     "quadrant": "low-low", "interpretation": "mock 双低"},
                    {"keyword": "meal prep containers", "click": 0.122, "conv": 0.033,
                     "quadrant": "high-low", "interpretation": "mock 点击高转化低"},
                    {"keyword": "kids backpack", "click": 0.076, "conv": 0.061,
                     "quadrant": "low-high", "interpretation": "mock 转化高"},
                ],
            },
            "opportunity_matrix": [{
                "keyword": sample[0],
                "type": "core",
                "evidence": {"searches": 10000, "relevance": "A"},
                "confidence": "mid",
            }],
            "product_hypotheses": [{
                "demand": "mock 需求",
                "source_keywords": [sample[0]],
                "evidence": {"searches": 10000, "purchase_rate": 0.02},
                "user_problem": "mock 用户问题（推断）",
                "product_form": "mock 形态",
                "material": "",
                "function": "",
                "spec": "",
                "bundle_accessory": "",
                "product_response": "mock 产品响应（假设）",
                "confidence": "mid",
                "inference_level": "hypothesis",
                "validation_needed": "mock 待验证",
            }],
            "sku_structure": {"main_form": "mock", "bundle_demand": "",
                              "real_accessories": [], "fake_accessories": [],
                              "sku_hypothesis": "mock SKU（假设）"},
            "seasonality": {"growth_start_month": "", "peak_months": [],
                            "peak_duration": "", "off_duration": "",
                            "two_year_repeat": False, "timeline_advice": "mock"},
            "lifecycle": {"classification": "mature", "evidence": "mock"},
            "entry_conclusion": {
                "grade": "VALIDATE",
                "answers": {f"q{i}": f"mock 回答 {i}" for i in range(1, 8)},
                "evidence": "mock 证据",
                "limitations": "mock 限制",
            },
            "risk_checklist": {"cannot_prove": ["mock"], "next_validations": ["mock"]},
        }

    def complete(self, messages: list[dict], *, max_tokens: int | None = None,
                 json_mode: bool = True) -> LLMResult:
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        if "关键词列表" in user:
            self.calls.append("label")
            text = json.dumps(self._label_response(json.loads(user)["关键词列表"]),
                              ensure_ascii=False)
        else:
            self.calls.append("analysis")
            try:
                payload = json.loads(user[user.find("{"):user.rfind("}") + 1])
                keywords = payload.get("keywords", [])
            except Exception:  # noqa: BLE001
                keywords = []
            text = json.dumps(self._analysis_response(keywords), ensure_ascii=False)
        return LLMResult(text, finish_reason="stop", model="fake", mode="chat")

    def test_connection(self) -> dict:
        return {"ok": True, "mode": "chat", "model": "fake", "sample": "正常"}


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, tmp_path):
    """离线护栏：只放行回环地址（asyncio 自管道需要），其余连接一律失败。"""
    loopback = ("127.0.0.1", "::1", "localhost")

    def _host(address):
        return address[0] if isinstance(address, tuple) else None

    def _guarded_connect(original):
        def _connect(self, address):
            if _host(address) not in loopback:
                raise AssertionError(f"测试不得发起真实网络连接: {address!r}")
            return original(self, address)
        return _connect

    monkeypatch.setattr(socket.socket, "connect",
                        _guarded_connect(socket.socket.connect))
    monkeypatch.setattr(socket, "create_connection",
                        _guarded_connect(socket.create_connection))
    # 凭据目录指向临时目录，不读写真实配置
    monkeypatch.setenv("TAG_PROVIDER_HOME", str(tmp_path / "provider-home"))
    yield


class _Store:
    """流水线只用 log / set_phase 两个方法。生产侧这两个方法由 runner 的运行登记实现，
    这里只记录调用，用来断言流水线确实驱动了存储接口。"""

    def __init__(self) -> None:
        self.phases: list[tuple[str, str]] = []
        self.lines: list[str] = []

    def set_phase(self, task_dir: Path, phase: str, message: str = "") -> None:
        self.phases.append((phase, message))

    def log(self, task_dir: Path, line: str) -> None:
        self.lines.append(line)


def _make_config() -> EngineConfig:
    # 限流放开（限流行为由 rate_limiter 自身测试覆盖），避免 22 次调用真实等待
    return EngineConfig(mcp_endpoint="", llm_base_url="", llm_api_key="",
                        llm_model="fake", window_max_calls=1000,
                        window_seconds=60.0, min_call_interval=0.0)


def _prepare_task(workspace: Path, asins: list[str]) -> Path:
    task_dir = workspace / "kw-run"
    for name in ("raw", "cache", "logs", "output"):
        (task_dir / name).mkdir(parents=True, exist_ok=True)
    input_data = {
        "groups": [{"marketplace": "US", "asins": asins, "node_ids": [], "node_top_n": 50}],
        "options": {"data_month": "202606", "keyword_pool_pages": 3,
                    "trend_months": 24, "core_trend_keywords": 5,
                    "longtail_trend_keywords": 5, "trend_keyword_cap": 20,
                    "current_ppc": True, "historical_ppc": False,
                    "report_language": "zh-CN"},
    }
    (task_dir / "input.json").write_text(
        json.dumps(input_data, ensure_ascii=False, indent=2), encoding="utf-8")
    (task_dir / "plan.json").write_text(json.dumps({
        "planned_total": 32, "hard_cap": 35, "confirmed": True,
        "confirmed_at": "2026-08-29T00:00:00+08:00",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return task_dir


def _make_pipeline(config: EngineConfig, task_dir: Path, store: _Store,
                   events: list[dict]) -> Pipeline:
    pipeline = Pipeline(config, task_dir, store, on_event=events.append,
                        llm=FakeLLM())
    pipeline.mcp = FakeMCP(FIXTURES, pipeline.rate_limiter, pipeline.call_log,
                           on_event=pipeline.on_event)
    return pipeline


def test_full_pipeline_offline_outputs_excel(tmp_path):
    """离线跑通完整流水线，并校验真的产出 13 Sheet 的报告。"""
    store = _Store()
    task_dir = _prepare_task(tmp_path / "ws", HIST_ASINS)
    events: list[dict] = []
    pipeline = _make_pipeline(_make_config(), task_dir, store, events)

    assert pipeline.run() == {"status": "done"}

    phases = [e["phase"] for e in events if e.get("kind") == "phase"]
    assert phases[-1] == "done"
    # 阶段与日志确实走了存储接口（生产侧这两个方法由运行登记实现）
    assert pipeline.store.phases[-1][0] == "done"
    assert pipeline.store.lines

    reports = list((task_dir / "output").glob("*.xlsx"))
    assert len(reports) == 1
    report = reports[0]
    assert report.stat().st_size > 0

    workbook = openpyxl.load_workbook(report)
    sheet_names = _engine_sheet_names()
    assert len(sheet_names) == 13
    assert workbook.sheetnames == sheet_names

    # 中间产物与调用日志齐全（真实调用被 FakeMCP 替代）
    for name in ("keyword_master.json", "keyword_labeled.json",
                 "trend_analysis.json", "analysis.json", "call_log.json"):
        assert (task_dir / "cache" / name).is_file()
    log = json.loads((task_dir / "cache" / "call_log.json").read_text(encoding="utf-8"))
    plan = json.loads((task_dir / "plan.json").read_text(encoding="utf-8"))
    assert log["actual_total"] == plan["planned_total"]
    assert all(c["status"] == "success" for c in log["calls"])


def test_cancel_from_other_thread(tmp_path):
    """cancel() 可被其它线程调用，run() 返回 canceled。"""
    store = _Store()
    task_dir = _prepare_task(tmp_path / "ws", HIST_ASINS)
    pipeline = _make_pipeline(_make_config(), task_dir, store, events=[])

    thread = threading.Thread(target=pipeline.cancel)
    thread.start()
    thread.join()

    assert pipeline.run() == {"status": "canceled"}
    assert not list((task_dir / "output").glob("*.xlsx"))
