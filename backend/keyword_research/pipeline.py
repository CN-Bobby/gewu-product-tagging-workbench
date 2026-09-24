# -*- coding: utf-8 -*-
"""任务流水线：skill v1.6.2 总流程 14 步的脚本化编排。

设计约束：
- 所有 SellerSprite 调用经全局限流器（默认30次/滚动60s、间隔至少1秒）与 call_log；
- MCP 返回先原样落盘 raw/ 再解析（skill 契约），raw/cache 即断点：产物存在则跳过；
- LLM 两步（标注、分析）在 llm_label.py / llm_analysis.py 中分批/分段执行；
- run() 为同步入口，供作业线程调用；取消通过 cancel() 置位（可被其它线程调用）。
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from . import engine_runner
from .budget import Budget, plan_budget
from .call_log import CallLog
from .config import EngineConfig
from .input_validator import validate_task
from .llm_analysis import LLMAnalyzer
from .llm_client import LLMClient
from .llm_label import LLMLabeler
from .mcp_client import MCPClient, MCPError, diagnostic_for_exception
from .rate_limiter import RateLimiter

OnEvent = Optional[Callable[[dict], None]]

NODE_TOP_N = 50
PAGE_SIZE = 50
KEYWORD_POOL_PAGES = 3


def _retry_budget_from_confirmed_plan(plan: dict) -> int:
    """Read current nested or legacy flattened plans (legacy default: 2)."""
    budget_data = plan.get("budget") if isinstance(plan.get("budget"), dict) else plan
    retry_budget = budget_data.get("retry_budget", plan.get("retry_budget", 2))
    try:
        return max(0, int(retry_budget))
    except (TypeError, ValueError):
        return 2


def _used_retries_from_call_log(call_log_data: dict) -> int:
    """Count every retry attempt already started, including failed/in-progress calls."""
    calls = call_log_data.get("calls", []) if isinstance(call_log_data, dict) else []
    if not isinstance(calls, list):
        return 0
    return sum(1 for call in calls if isinstance(call, dict) and call.get("is_retry"))


class PipelineCanceled(Exception):
    pass


class Pipeline:
    # store 只需两个方法：log(task_dir, line) 与 set_phase(task_dir, phase, message)。
    # 工作台侧由运行登记实现，引擎不关心状态写到哪个文件。
    def __init__(self, config: EngineConfig, task_dir: Path, store,
                 on_event: OnEvent = None, llm: LLMClient | None = None,
                 mcp: MCPClient | None = None,
                 cancel_event: threading.Event | None = None) -> None:
        self.config = config
        self.task_dir = Path(task_dir)
        self.store = store
        self.on_event = on_event
        # threading.Event：cancel() 由 HTTP 线程调用，asyncio.Event 跨线程 set() 不安全。
        # 上层可以把自己的事件交进来，这样取消只有一个真相源，不需要桥接线程。
        self._cancel = threading.Event() if cancel_event is None else cancel_event

        self.rate_limiter = RateLimiter(
            max_calls=config.window_max_calls,
            window_seconds=config.window_seconds,
            min_interval=config.min_call_interval,
        )
        self.call_log = CallLog(self.task_dir / "cache" / "call_log.json")
        if mcp is None:
            mcp = MCPClient(config.mcp_endpoint, self.rate_limiter,
                            self.call_log, on_event=self.on_event)
        self.mcp = mcp
        if llm is None:
            llm = LLMClient(config.llm_base_url, config.llm_api_key,
                            config.llm_model, mode=config.llm_mode,
                            temperature=config.llm_temperature,
                            max_tokens=config.llm_max_tokens,
                            timeout=config.llm_request_timeout)
        self.llm = llm
        self.labeler = LLMLabeler(self.llm, on_event=self.on_event)
        self.analyzer = LLMAnalyzer(self.llm, on_event=self.on_event)

    # ---------- 工具 ----------

    def _emit(self, kind: str, **payload) -> None:
        if self.on_event is not None:
            try:
                self.on_event({"kind": kind, **payload})
            except Exception:
                pass

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise PipelineCanceled()

    def cancel(self) -> None:
        self._cancel.set()

    def _log(self, line: str) -> None:
        self.store.log(self.task_dir, line)
        self._emit("log", line=line)

    def _phase(self, phase: str, message: str) -> None:
        self.store.set_phase(self.task_dir, phase, message)
        self._emit("phase", phase=phase, message=message)

    def _save_raw(self, name: str, data: dict, meta: dict) -> Path:
        payload = dict(data)
        payload["_meta"] = meta
        path = self.task_dir / "raw" / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return path

    def _load_input(self) -> dict:
        path = self.task_dir / "input.json"
        if not path.exists():
            raise RuntimeError("缺少 input.json：请先在界面创建任务")
        return json.loads(path.read_text(encoding="utf-8"))

    def _load_plan(self) -> dict:
        path = self.task_dir / "plan.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    # ---------- 主流程 ----------

    def run(self) -> dict:
        try:
            return asyncio.run(self._run_async())
        except PipelineCanceled:
            self._phase("canceled", "任务已取消（已完成的 raw/cache 保留，可重试续跑）")
            return {"status": "canceled"}
        except Exception as exc:  # noqa: BLE001
            message=diagnostic_for_exception(exc)
            self._log(f"任务失败: {message}")
            self._phase("failed", message[:300])
            self._emit("pipeline_error", error=message)
            raise RuntimeError(message) from None

    async def _run_async(self) -> dict:
        input_data = self._load_input()
        plan = self._load_plan()
        if not plan.get("confirmed"):
            raise RuntimeError("预算未确认：请先核算并确认预算")
        # Current plans keep the budget under `budget`; older plans may be flat.
        retry_budget = _retry_budget_from_confirmed_plan(plan)
        set_retry_budget = getattr(self.mcp, "set_task_retry_budget", None)
        if callable(set_retry_budget):
            retries_used = _used_retries_from_call_log(self.call_log.load())
            set_retry_budget(retry_budget, retries_used=retries_used)

        self._check_cancel()
        self._phase("collecting", "开始数据采集")
        asin_pool = await self._step_nodes_and_pool(input_data)
        await self._step_traffic_extend(input_data, asin_pool)
        options = input_data.get("options") or {}
        if bool(options.get("matrix_enabled", True)):
            await self._step_traffic_keyword(input_data)
        else:
            self._log("本次未勾选矩阵，跳过逐 ASIN 流量词矩阵（不调用 traffic_keyword，不产出 08/09 两张矩阵表）")

        self._check_cancel()
        self._phase("aggregating", "本地聚合与标准化")
        self._run_local_steps(input_data)

        self._check_cancel()
        self._phase("labeling", "LLM 翻译与标签分类")
        await self._step_labeling()

        self._check_cancel()
        self._phase("trends", "趋势词选取与历史拉取")
        trend_keywords = self._step_trend_select()
        await self._step_trend_fetch(trend_keywords, input_data)
        self._run_script("analyze_trends.py")

        self._check_cancel()
        self._phase("analyzing", "LLM 市场分析（三段）")
        await self._step_analysis()

        self._check_cancel()
        self._phase("building", "生成 Excel 报告")
        await asyncio.to_thread(self._step_build_report)
        self._phase("validating", "报告验收")
        await asyncio.to_thread(self._step_validate_report)

        self._phase("done", "任务完成")
        self._log("全流程完成")
        return {"status": "done"}

    # ---------- 步骤 1-2：Node 校验与榜单取样 → ASIN 池 ----------

    async def _step_nodes_and_pool(self, input_data: dict) -> list[dict]:
        """返回 asin_pool（含来源追踪）。仅 Node 输入时调用 MCP。"""
        pool: list[dict] = []
        groups = input_data.get("groups", [])
        month = (input_data.get("options") or {}).get("data_month")
        for g in groups:
            mp = g["marketplace"]
            # 直接输入 ASIN 先入池
            for a in g.get("asins", []):
                pool.append({"asin": a, "sources": ["input"]})
            for node_id in g.get("node_ids", []):
                self._check_cancel()
                node_file = self.task_dir / "raw" / f"node_{mp}_{node_id}.json"
                if node_file.exists():
                    node_data = json.loads(node_file.read_text(encoding="utf-8"))
                    self._log(f"复用已缓存 Node 校验: {node_id} ({mp})")
                else:
                    self._log(f"校验 Node: {node_id} ({mp})")
                    node_data = await self.mcp.product_node(mp, node_id, month)
                    self._save_raw(f"node_{mp}_{node_id}.json", node_data,
                                   {"marketplace": mp, "node_id": node_id,
                                    "called_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
                path = self._extract_node_path(node_data, node_id)
                if not path:
                    self._log(f"警告: Node {node_id} 未返回路径，跳过榜单取样")
                    continue
                self._log(f"Node {node_id} 路径: {path}")
                pool.extend(await self._node_products(mp, node_id, path, month,
                                                      g.get("node_top_n", NODE_TOP_N)))
        return pool

    def _extract_node_path(self, node_data: dict, node_id: str) -> str | None:
        """从 product_node 返回中提取 nodeIdPath。"""
        d = node_data.get("data") or node_data
        if not isinstance(d, dict):
            return None
        # 常见字段：nodeIdPath / nodePath / path
        for key in ("nodeIdPath", "nodePath", "path"):
            v = d.get(key)
            if isinstance(v, str) and v.strip():
                return v
        items = d.get("items")
        if isinstance(items, list) and items:
            for key in ("nodeIdPath", "nodePath", "path"):
                v = items[0].get(key)
                if isinstance(v, str) and v.strip():
                    return v
        # 备用：原样回传 node id 字符串
        return None

    async def _node_products(self, mp: str, node_id: str, path: str,
                             month: str | None, top_n: int = NODE_TOP_N) -> list[dict]:
        """product_research 榜单取样：本地复核子类 BSR，取前 node_top_n。"""
        out: list[dict] = []
        top_n = top_n if top_n in (50, 100) else NODE_TOP_N
        page = 1
        # 固定最多 2 页（Top50 一页即可；不主动翻页过度消耗）
        while len(out) < top_n and page <= (top_n + PAGE_SIZE - 1) // PAGE_SIZE:
            self._check_cancel()
            fname = f"products_{mp}_{node_id}_p{page}.json"
            fpath = self.task_dir / "raw" / fname
            if fpath.exists():
                data = json.loads(fpath.read_text(encoding="utf-8"))
            else:
                data = await self.mcp.product_research(mp, path, month or _this_month(), page)
                self._save_raw(fname, data, {"marketplace": mp, "node_id": node_id,
                                             "page": page, "called_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
            items = self._extract_items(data)
            if not items:
                break
            for it in items:
                if len(out) >= top_n:
                    break
                asin = it.get("asin")
                if not asin:
                    continue
                rank = self._sub_bsr_rank(it)
                if rank is not None and rank > top_n:
                    continue  # 本地复核：超出 Top N 排除
                out.append({
                    "asin": asin,
                    "parent": it.get("parent") or it.get("parentAsin"),
                    "sources": [f"node:{node_id}"],
                    "sub_bsr": rank,
                    "monthly_sales": it.get("monthlySales") or it.get("totalUnits"),
                    "brand": it.get("brand", ""),
                    "title": (it.get("title") or "")[:200],
                })
            page += 1
        self._log(f"Node {node_id} 榜单取样 {len(out)} 个 ASIN")
        return out

    @staticmethod
    def _sub_bsr_rank(item: dict) -> int | None:
        subs = item.get("subcategories")
        if isinstance(subs, list):
            for s in subs:
                r = s.get("rank")
                if isinstance(r, (int, float)) and r > 0:
                    return int(r)
        r = item.get("subBsrRank")
        if isinstance(r, (int, float)) and r > 0:
            return int(r)
        return None

    @staticmethod
    def _extract_items(data: dict) -> list:
        d = data.get("data") or data
        if isinstance(d, dict):
            items = d.get("items")
            if isinstance(items, list):
                return items
            # MCP 包装：content[0].text
            for v in d.values():
                if isinstance(v, dict) and isinstance(v.get("items"), list):
                    return v["items"]
        return []

    # ---------- 步骤 3：traffic_extend 分批反查 ----------

    async def _step_traffic_extend(self, input_data: dict, pool: list[dict]) -> None:
        groups = input_data.get("groups", [])
        month = (input_data.get("options") or {}).get("data_month")
        batch_no = 0  # 全局批次编号（跨 group 递增），文件名须匹配脚本正则
        for gi, g in enumerate(groups, start=1):
            mp = g["marketplace"]
            direct = g.get("asins", [])
            batches: list[tuple[int, list[str]]] = []  # (批内序号, asins)
            if len(direct) == 1 and g.get("node_ids"):
                # M1：输入 ASIN 单独批次（来源归因）
                batches.append((1, direct))
            # 池内 ASIN（去重、保持顺序），每批 ≤20
            seen: set[str] = set()
            ordered: list[str] = []
            for p in pool:
                if p["asin"] not in seen:
                    seen.add(p["asin"])
                    ordered.append(p["asin"])
            covered = {a for _, lst in batches for a in lst}
            rest = [a for a in ordered if a not in covered]
            if rest:
                for i in range(0, len(rest), 20):
                    batches.append((len(batches) + 1, rest[i:i + 20]))
            if not batches:
                batches.append((1, ordered))

            for bi, asins in batches:
                batch_no += 1
                for page in range(1, KEYWORD_POOL_PAGES + 1):
                    self._check_cancel()
                    fname = f"traffic_batch_{batch_no}_p{page}.json"
                    fpath = self.task_dir / "raw" / fname
                    if fpath.exists():
                        self._log("复用已缓存的流量反查结果")
                        continue
                    self._log(f"反查 {mp} 批次{batch_no}（{len(asins)} ASIN）第{page}/3页")
                    data = await self.mcp.traffic_extend(
                        mp, asins, page, size=PAGE_SIZE, history_date=month)
                    self._save_raw(fname, data, {
                        "marketplace": mp, "group": gi, "batch": batch_no,
                        "page": page, "asins": asins,
                        "called_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    })
                    await asyncio.sleep(0)

        # 防呆：指定了月份但反查全部 total=0 → 服务端快照不可用（SellerSprite 机制）。
        # 明确报错并引导改月份，避免"词池为空"的笼统提示。
        if month:
            batch_files = sorted(self.task_dir.glob("raw/traffic_batch_*.json"))
            if batch_files:
                all_zero = True
                for f in batch_files:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    data = d.get("data") or {}
                    if (data.get("total") or 0) > 0:
                        all_zero = False
                        break
                if all_zero:
                    now = datetime.now()
                    if month == now.strftime("%Y%m"):
                        prev = (now.replace(day=1) - timedelta(days=1)).strftime("%Y%m")
                        raise RuntimeError(
                            f"防呆：{month} 为当前月份，SellerSprite 服务端快照尚未归档，"
                            f"反查返回 0 词。建议改选已归档月份（如 {prev}）或下月初归档后重跑。")
                    raise RuntimeError(
                        f"防呆：{month} 反查未返回任何关键词（total=0）。"
                        f"请确认月份格式（yyyyMM）与 ASIN 有效性，或改选其他月份。")

    # ---------- 步骤 4：traffic_keyword 逐 ASIN 当前矩阵 ----------

    async def _step_traffic_keyword(self, input_data: dict) -> None:
        groups = input_data.get("groups", [])
        options = input_data.get("options") or {}
        month = options.get("matrix_month")  # None=近30天实时（不跟随汇总月）
        for g in groups:
            mp = g["marketplace"]
            for asin in g.get("asins", []):
                self._check_cancel()
                fname = f"traffic_keyword_current_{asin}_p1.json"
                fpath = self.task_dir / "raw" / fname
                if fpath.exists():
                    self._log("复用已缓存的流量矩阵")
                    continue
                snap = f" {month}" if month else ""
                self._log(f"当前流量矩阵 {mp}{snap}（前100词）")
                data = await self.mcp.traffic_keyword(mp, asin, page=1, size=100,
                                                      month=month or None)
                self._save_raw(fname, data, {
                    "asin": asin, "marketplace": mp,
                    "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "page": 1, "page_size": 100, "snapshot_type": "current",
                    "month": month,
                })
                await asyncio.sleep(0)

        # 防呆：指定了矩阵月份但全部矩阵 total=0 → 该月快照不可用（SellerSprite 机制）。
        # 与反查防呆同款：当月明确报错引导改月份；历史月报无数据。
        if month:
            matrix_files = sorted(self.task_dir.glob("raw/traffic_keyword_current_*.json"))
            if matrix_files:
                all_zero = True
                for f in matrix_files:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    data = d.get("data") or {}
                    if (data.get("total") or 0) > 0:
                        all_zero = False
                        break
                if all_zero:
                    now = datetime.now()
                    if month == now.strftime("%Y%m"):
                        prev = (now.replace(day=1) - timedelta(days=1)).strftime("%Y%m")
                        raise RuntimeError(
                            f"防呆：矩阵月份 {month} 为当前月份，SellerSprite 服务端快照尚未归档，"
                            f"所有 ASIN 矩阵均返回 0 词。建议改选已归档月份（如 {prev}）或下月初归档后重跑。")
                    raise RuntimeError(
                        f"防呆：矩阵月份 {month} 所有 ASIN 均未返回流量词（total=0）。"
                        f"请确认月份格式（yyyyMM）与 ASIN 有效性，或改选其他月份。")

    # ---------- 步骤 5：本地聚合脚本 ----------

    def _run_local_steps(self, input_data: dict) -> None:
        self._run_script("aggregate_batches.py")
        self._run_script("normalize_keywords.py")
        self._run_script("aggregate_asin_keyword_matrix.py")
        # 防御：词池为空直接报错（可能服务端限流被吞或 ASIN 无效），
        # 避免后续 select_trend 给出误导性"无候选关键词"
        master = json.loads((self.task_dir / "cache" / "keyword_master.json")
                            .read_text(encoding="utf-8"))
        n = len(master.get("keywords", []))
        if n == 0:
            raw_rows = master.get("keyword_pool_raw_rows", "?")
            detail = (
                f"（反查原始行数={raw_rows}，词池实际={master.get('keyword_pool_actual', '?')}；"
                f"若指定了月份，可能是该月快照无数据或当月未归档）")
            raise RuntimeError(
                "反查未返回任何关键词（词池为空）。可能原因：SellerSprite 服务端限流"
                f"（如“接口访问次数已达上限”，可在日志/报告确认）、或输入 ASIN 无流量数据。{detail}"
                "建议稍后重试或更换 ASIN。")

    def _run_script(self, name: str, *extra: str) -> str:
        self._check_cancel()
        self._log(f"运行引擎脚本: {name}")
        try:
            return engine_runner.run_script(
                name, self.task_dir, *extra,
                on_output=lambda o: self._log(o.strip()[-200:] if o.strip() else ""),
                cancel=self._cancel,
            )
        except engine_runner.EngineCancelled as exc:
            raise PipelineCanceled() from exc

    # ---------- 步骤 6-7：LLM 标注 + 校验 ----------

    async def _step_labeling(self) -> None:
        labeled_path = self.task_dir / "cache" / "keyword_labeled.json"
        if not labeled_path.exists():
            raise RuntimeError("缺少 keyword_labeled.json：本地聚合步骤未完成")
        # 全部已标注则跳过
        data = json.loads(labeled_path.read_text(encoding="utf-8"))
        pending = [k for k in data.get("keywords", []) if not k.get("translation_fixed")]
        if not pending:
            self._log("关键词已全部标注，跳过 LLM 标注")
        else:
            self._log(f"LLM 标注 {len(pending)} 个关键词（分批进行）")
            stats = await asyncio.to_thread(self.labeler.label_file, labeled_path)
            self._log(f"标注完成: {stats}")
        self._run_script("classify_keywords.py")
        self._run_script("validate_labels.py")

    # ---------- 步骤 8-9：趋势 ----------

    def _step_trend_select(self) -> list[dict]:
        self._run_script("select_trend_keywords.py")
        path = self.task_dir / "cache" / "trend_keywords.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        trend_keywords = data.get("keywords", []) if isinstance(data, dict) else data
        # validate_report 按实际趋势词数校验 plan.planned_total，须在此更新
        self._update_plan_with_actual(len(trend_keywords))
        return trend_keywords

    def _update_plan_with_actual(self, trend_count: int) -> None:
        """趋势词确定后，用实际 T 重算预算并更新 plan.json。"""
        input_data = self._load_input()
        plan = self._load_plan()
        budget = plan_budget(input_data.get("groups", []),
                             input_data.get("options", {}))
        for gb in budget.groups:
            gb.trend_calls = trend_count
            # 矩阵调用数取自 plan_budget：未勾选矩阵时为 0，不得在此另判一次开关
            gb.total = (gb.pool_batches * KEYWORD_POOL_PAGES + gb.matrix_calls
                        + trend_count + gb.extra_m1
                        + (gb.node_pages + gb.node_count if gb.node_count else 0))
        plan["planned_total"] = budget.planned_total
        plan["hard_cap"] = budget.effective_hard_cap
        plan["actual_trend_keywords"] = trend_count
        plan["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        (self.task_dir / "plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        self._log(f"计划更新：实际趋势词 {trend_count} 个，总调用 {budget.planned_total} 次")

    async def _step_trend_fetch(self, trend_keywords: list[dict], input_data: dict) -> None:
        mp = (input_data.get("groups") or [{}])[0].get("marketplace", "")
        for item in trend_keywords:
            self._check_cancel()
            keyword = item.get("keyword") or item.get("词") or ""
            if not keyword:
                continue
            slug = _slug(keyword)
            fname = f"trend_{mp}_{slug}.json"
            fpath = self.task_dir / "raw" / fname
            if fpath.exists():
                self._log(f"复用趋势缓存: {keyword}")
                continue
            self._log(f"拉取趋势: {keyword}")
            data = await self.mcp.aba_research_trend(mp, keyword)
            self._save_raw(fname, data, {"marketplace": mp, "keyword": keyword,
                                         "called_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
            await asyncio.sleep(0)

    # ---------- 步骤 10-11：LLM 分析 ----------

    async def _step_analysis(self) -> None:
        from .llm_analysis import _normalize_analysis

        analysis_path = self.task_dir / "cache" / "analysis.json"
        if analysis_path.exists():
            data = json.loads(analysis_path.read_text(encoding="utf-8"))
            # 幂等规整：修复历史数据中 evidence 等字段类型漂移（真实 LLM 输出不可控）
            _normalize_analysis(data)
            analysis_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            if data.get("analysis_mode") == "model_split":
                self._log("分析三段完整，跳过 LLM 分析")
                return
        # hybrid/deterministic 均重跑：analyzer 内部只重跑失败段（段级续跑）
        await asyncio.to_thread(self.analyzer.analyze, self.task_dir)
        self._run_script("validate_analysis.py")

    # ---------- 步骤 12-13：Excel ----------

    def _step_build_report(self) -> None:
        # 模板放在实例数据目录、缺时现场生成（不进版本库，也不写程序目录）
        self._run_script("build_excel_report.py", "--template",
                         str(engine_runner.template_path()))

    def _step_validate_report(self) -> None:
        self._run_script("validate_report.py")


def _slug(keyword: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", keyword.lower()).strip("_")
    return s or "kw"


def _this_month() -> str:
    return time.strftime("%Y%m")


def prepare_task_input(groups: list[dict], month: str | None,
                       matrix_month: str | None = None,
                       options: dict | None = None) -> dict:
    """校验并组装 input.json 结构（创建任务时调用）。

    matrix_month=None → 矩阵跟随 data_month（不写入 options）。
    """
    task_input = validate_task(groups, month, matrix_month=matrix_month)
    opts = {
        "data_month": task_input["options"]["data_month"],
        "keyword_pool_pages": KEYWORD_POOL_PAGES,
        "trend_months": 24,
        "core_trend_keywords": 5,
        "longtail_trend_keywords": 5,
        "trend_keyword_cap": 20,
        "current_ppc": True,
        "historical_ppc": False,
        "report_language": "zh-CN",
    }
    if task_input["options"].get("matrix_month"):
        opts["matrix_month"] = task_input["options"]["matrix_month"]
    if options:
        opts.update({k: v for k, v in options.items() if v is not None})
    task_input["options"] = opts
    return task_input


def build_plan(input_data: dict) -> Budget:
    """按 input.json 计算预算（供界面展示）。"""
    return plan_budget(input_data.get("groups", []), input_data.get("options", {}))
