# -*- coding: utf-8 -*-
"""LLM 翻译校正 + 多标签分类（skill 步骤 7，原为 LLM 人工步骤）。

容量设计（需求 3）：
- 关键词分批（默认 40 词/批）调用 LLM，JSON 结构化输出；
- finish_reason=length 截断时把该批拆半重试，只重跑失败批；
- 已标注的词（translation_fixed 存在）跳过，支持断点续跑；
- 输出字段严格对齐 references/keyword-taxonomy.md §3 字段契约。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable, Optional

from .llm_client import LLMClient, LLMError

OnEvent = Optional[Callable[[dict], None]]

DEFAULT_BATCH_SIZE = 10
MAX_BATCH_RETRIES = 2
# 单批次小于该数量时不再拆半（继续拆已无意义）
MIN_SPLIT_SIZE = 6

SYSTEM_PROMPT = """你是亚马逊市场关键词调研的资深分析师。对给定关键词列表逐词完成：
1. 中文翻译校正：MCP 原译 keywordCn 可能生硬或错误，给出校正后的中文 translation_fixed（品牌词保留原文），并给出简短理由与置信度。
2. 词族 family：把同义词族归为一个简短中文词族名（如“午餐盒”“保温杯”）。
3. 需求属性标签 demand_tags：从下列前缀中选择适用者（可多选），值尽量具体：
   core_category(核心品类), audience(人群), scene(场景), occasion(场合), material(材料),
   function(功能), spec(规格/尺寸/数量), gift(礼品语义), bundle(套装), accessory(配件), style(风格), brand(品牌名)
   格式为 "前缀:值"，如 ["material:rattan", "spec:9inch"]。品牌词在 demand_tags 中加 "brand:品牌名" 并设 is_brand=true。
4. 相关度 relevance（A/B/C/D，附一句可审计理由 relevance_reason）：
   A=与调研品类高度相关且购买意图明确；B=相关但跨品类或泛化；C=场景/品类关联但购买意图弱；D=无关/噪声。
5. 购买意图 purchase_intent：高/中/低（语义判断，与 purchaseRate 数据字段无关）。
6. is_noise：是否噪声词（无关、错类）。
7. local_context：仅当关键词有站点本地语义值得备注时填写，否则空字符串。

【输出体积约束（重要）】translation_reason 与 relevance_reason 必须 ≤12 个汉字，
用短语而非完整句子（如"直译准确""人群前置"）；relevance_reason 用"品类相关""跨品类""购买意图弱"等短语。

严格输出 JSON：一个数组，元素字段固定为
["keyword","translation_fixed","translation_reason","translation_confidence","family",
 "demand_tags","relevance","relevance_reason","purchase_intent","is_brand","is_noise","local_context"]
其中 translation_confidence 取 high/medium/low，demand_tags 为字符串数组。
不要输出任何数组以外的内容。"""


def build_batch_prompt(keywords: list[dict]) -> str:
    """把一批关键词压缩为紧凑 JSON 供 LLM 标注。"""
    rows = []
    for k in keywords:
        rows.append({
            "keyword": k.get("keyword", ""),
            "keywordCn": k.get("keywordCn", ""),
            "searches": k.get("searches"),
            "family_guess": k.get("family_guess", ""),
            "is_brand_candidate": bool(k.get("is_brand_candidate")),
            "marketplace": k.get("marketplace", ""),
        })
    return json.dumps({"关键词列表": rows}, ensure_ascii=False)


def parse_label_output(text: str) -> list[dict]:
    """解析 LLM 输出数组；校验字段名与必填项。"""
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        last = text.rfind("```")
        text = text[first_nl + 1:last].strip()
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("LLM 标注输出不是数组")
    required = {"keyword", "translation_fixed", "translation_reason",
                "translation_confidence", "family", "demand_tags",
                "relevance", "relevance_reason", "purchase_intent"}
    for row in data:
        if not isinstance(row, dict):
            raise ValueError("LLM 标注行不是对象")
        missing = required - set(row.keys())
        if missing:
            raise ValueError(f"LLM 标注行缺少字段: {sorted(missing)}")
        if row.get("relevance") not in ("A", "B", "C", "D"):
            raise ValueError(f"relevance 非法: {row.get('relevance')!r}")
        # 购买意图归一化：容忍模型输出英文枚举（实测出现过 high/medium/low）
        pi = str(row.get("purchase_intent") or "").strip().lower()
        pi_map = {"高": "高", "中": "中", "低": "低",
                  "high": "高", "medium": "中", "low": "低"}
        if pi not in pi_map:
            raise ValueError(f"purchase_intent 非法: {row.get('purchase_intent')!r}")
        row["purchase_intent"] = pi_map[pi]
    return data


class LLMLabeler:
    def __init__(self, llm: LLMClient, on_event: OnEvent = None,
                 batch_size: int = DEFAULT_BATCH_SIZE,
                 max_batch_retries: int = MAX_BATCH_RETRIES) -> None:
        self.llm = llm
        self.on_event = on_event
        self.batch_size = batch_size
        self.max_batch_retries = max_batch_retries

    def _emit(self, kind: str, **payload) -> None:
        if self.on_event is not None:
            try:
                self.on_event({"kind": kind, **payload})
            except Exception:
                pass

    def label_file(self, labeled_path: Path, concurrency: int = 3) -> dict:
        """标注 cache/keyword_labeled.json 中未标注的关键词，就地写回。

        concurrency：并发批次数（DeepSeek 无速率限制，可并行；默认 3 路）。
        每批完成立即原子写回文件，进程中断后重跑只补未标注批次。
        """
        data = json.loads(labeled_path.read_text(encoding="utf-8"))
        keywords = data.get("keywords", [])
        pending = [k for k in keywords if not k.get("translation_fixed")]
        done = len(keywords) - len(pending)
        stats = {"total": len(keywords), "already": done, "labeled": 0,
                 "retries": 0, "split_batches": 0, "failed_batches": 0,
                 "completed_batches": 0}
        stats_lock = threading.Lock()
        write_lock = threading.Lock()

        # 按原顺序分批（保持 keyword 稳定排序）
        by_keyword = {k["keyword"]: k for k in keywords}
        order = [k["keyword"] for k in keywords if not k.get("translation_fixed")]

        batches = [order[i:i + self.batch_size]
                   for i in range(0, len(order), self.batch_size)]

        def _save() -> None:
            with write_lock:
                with stats_lock:
                    stats_copy = dict(stats)
                data["labeled_stats"] = stats_copy
                tmp = labeled_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                               encoding="utf-8")
                tmp.replace(labeled_path)

        def _work(bi: int, batch_keys: list[str]):
            try:
                self._label_batch(batch_keys, by_keyword, stats, stats_lock,
                                  write_lock, bi, len(batches))
                return None
            except Exception as exc:  # noqa: BLE001
                return (bi, str(exc))
            finally:
                with stats_lock:
                    stats["completed_batches"] += 1
                _save()  # 每批完成立即写回（断点续跑粒度）

        if concurrency <= 1:
            failures = [_work(bi, bk) for bi, bk in enumerate(batches, start=1)]
        else:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = [pool.submit(_work, bi, bk)
                           for bi, bk in enumerate(batches, start=1)]
                failures = [f.result() for f in futures]
        failures = [f for f in failures if f]
        data["labeled_stats"] = stats
        _save()
        if failures:
            raise RuntimeError(f"标签批次失败: {failures}")
        return stats

    def _label_batch(self, batch_keys: list[str], by_keyword: dict,
                     stats: dict, stats_lock: threading.Lock,
                     write_lock: threading.Lock,
                     bi: int, total_batches: int) -> None:
        """标注一批；截断拆半递归，失败重试。stats 更新须持锁。"""
        batch = [by_keyword[k] for k in batch_keys]
        self._emit("label_batch_start", batch_index=bi, total_batches=total_batches,
                   size=len(batch_keys))
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_batch_prompt(batch)},
        ]
        last_error: Exception | None = None
        for attempt in range(self.max_batch_retries + 1):
            try:
                result = self.llm.complete(messages, json_mode=True)
                if result.truncated:
                    raise LLMError("输出截断（finish_reason=length）", retryable=True)
                rows = parse_label_output(result.text)
                if len(rows) < len(batch_keys):
                    # 部分缺失：允许少 1 条（保守），否则视为失败
                    if len(rows) < len(batch_keys) - 1:
                        raise ValueError(
                            f"返回 {len(rows)} 条，期望 {len(batch_keys)} 条")
                with stats_lock:
                    stats["labeled"] += len(rows)
                self._apply_rows(rows, batch_keys, by_keyword, write_lock)
                self._emit("label_batch_done", batch_index=bi, size=len(batch_keys))
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                with stats_lock:
                    stats["retries"] += 1
                self._emit("label_batch_retry", batch_index=bi, attempt=attempt + 1,
                           error=str(exc)[:200])
                is_truncated = isinstance(exc, LLMError) and exc.truncated
                if is_truncated and len(batch_keys) > MIN_SPLIT_SIZE:
                    # 截断 → 拆半递归，各自独立失败不影响另一半
                    with stats_lock:
                        stats["split_batches"] += 1
                    mid = len(batch_keys) // 2
                    failures: list[str] = []
                    for half in (batch_keys[:mid], batch_keys[mid:]):
                        try:
                            self._label_batch(half, by_keyword, stats, stats_lock,
                                              write_lock, bi, total_batches)
                        except Exception as half_exc:  # noqa: BLE001
                            failures.append(str(half_exc))
                    if not failures:
                        return
                    last_error = RuntimeError("；".join(failures))
                    break
        with stats_lock:
            stats["failed_batches"] += 1
        raise RuntimeError(f"标签批次 {bi} 标注失败: {last_error}") from last_error

    def _apply_rows(self, rows: list[dict], batch_keys: list[str],
                    by_keyword: dict, write_lock: threading.Lock) -> None:
        # 与 _save 的 json.dumps 互斥：并发写字段可能触发
        # "dictionary changed size during iteration"（实测坑点）
        with write_lock:
            for row in rows:
                kw = row.get("keyword", "")
                target = by_keyword.get(kw)
                if target is None:
                    continue
                for fld in ("translation_fixed", "translation_reason",
                            "translation_confidence", "family", "demand_tags",
                            "relevance", "relevance_reason", "purchase_intent",
                            "is_brand", "is_noise", "local_context"):
                    if fld in row:
                        target[fld] = row[fld]
