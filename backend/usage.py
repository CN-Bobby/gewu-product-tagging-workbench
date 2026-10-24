"""Unified, append-only model and MCP usage ledger for this workbench instance."""
from __future__ import annotations

import math
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError, OperationalError

from .db import (
    Dataset, Session, UsageEvent, UsageTaskLink, Workspace, BatchJob, Run,
    now, uid,
)

DEFAULT_TIMEZONE = "Asia/Shanghai"
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_LATIN_WORD = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*")
_SENSITIVE_META_KEY = re.compile(
    r"(?:api.?key|secret|password|credential|authorization|cookie|prompt|payload|request|response|body|content|token)",
    re.IGNORECASE,
)


def _safe_metadata(value: object, depth: int = 0):
    """Keep report metadata small and prevent credentials or request contents leaking."""
    if depth > 4:
        return None
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:80]:
            name = str(key)[:80]
            if _SENSITIVE_META_KEY.search(name):
                continue
            safe = _safe_metadata(item, depth + 1)
            if safe is not None:
                result[name] = safe
        return result
    if isinstance(value, (list, tuple)):
        return [safe for item in list(value)[:80]
                if (safe := _safe_metadata(item, depth + 1)) is not None]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:500]
    return str(value)[:500]


def configured_timezone() -> ZoneInfo:
    name = (os.environ.get("GEWU_TIMEZONE") or DEFAULT_TIMEZONE).strip()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TIMEZONE)


def environment_name() -> str:
    return (os.environ.get("GEWU_PROFILE") or os.environ.get("GEWU_INSTANCE_ID") or "local").strip().lower()[:32]


def instance_identity() -> str:
    # Stable per data-directory instance, without exposing its absolute path.
    from .db import STORAGE
    import hashlib
    value = str(STORAGE).casefold().encode("utf-8", "replace")
    return hashlib.sha256(value).hexdigest()[:24]


def provider_name(model: str | None = None, endpoint: str | None = None) -> str | None:
    ref = str(model or "").strip()
    if "/" in ref:
        return ref.split("/", 1)[0].casefold()[:120]
    host = urlparse(str(endpoint or "")).hostname or ""
    for name in ("deepseek", "qwen", "aliyun", "dashscope", "openai", "anthropic"):
        if name in host.casefold():
            return name
    return None


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
        return number if number >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _first_number(*values):
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def normalize_provider_usage(usage: object) -> dict:
    """Normalize known OpenAI-compatible usage formats without filling absent fields."""
    if not isinstance(usage, dict):
        return {}
    input_tokens = _first_number(usage.get("prompt_tokens"), usage.get("input_tokens"))
    output_tokens = _first_number(usage.get("completion_tokens"), usage.get("output_tokens"))
    prompt_details = usage.get("prompt_tokens_details") or usage.get("prompt_token_details") or {}
    input_details = usage.get("input_tokens_details") or usage.get("input_token_details") or {}
    if not isinstance(prompt_details, dict):
        prompt_details = {}
    if not isinstance(input_details, dict):
        input_details = {}
    cache_hit = _first_number(
        usage.get("prompt_cache_hit_tokens"), usage.get("cached_tokens"),
        usage.get("cache_read_input_tokens"),
        prompt_details.get("cached_tokens"), prompt_details.get("cache_read_input_tokens"),
        input_details.get("cached_tokens"), input_details.get("cache_read_input_tokens"),
    )
    cache_miss = _first_number(
        usage.get("prompt_cache_miss_tokens"), usage.get("uncached_tokens"),
        prompt_details.get("uncached_tokens"), input_details.get("uncached_tokens"),
    )
    cache_create = _first_number(
        usage.get("cache_creation_input_tokens"), usage.get("cache_creation_tokens"),
        prompt_details.get("cache_creation_input_tokens"),
        input_details.get("cache_creation_input_tokens"),
    )
    prompt_total = _number(usage.get("prompt_tokens"))
    if (cache_miss is None and cache_create is None and prompt_total is not None
            and cache_hit is not None and cache_hit <= prompt_total):
        # Provider-reported total input and hit count make the non-cached portion derivable.
        cache_miss = prompt_total - cache_hit
    normalized = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_hit_tokens": cache_hit,
        "cache_miss_tokens": cache_miss,
        "cache_creation_tokens": cache_create,
    }
    return {key: value for key, value in normalized.items() if value is not None}


def _texts(value: object, *, _key: str = "") -> list[str]:
    if isinstance(value, str):
        low = value.lstrip().lower()
        if low.startswith(("data:image/", "data:application/")) or len(value) > 2_000_000:
            return []
        return [value]
    if isinstance(value, dict):
        out = []
        for key, item in value.items():
            key = str(key).casefold()
            if key in {"image_url", "image", "input_image", "audio", "file", "data"}:
                continue
            out.extend(_texts(item, _key=key))
        return out
    if isinstance(value, (list, tuple)):
        return [piece for item in value for piece in _texts(item, _key=_key)]
    return []


def _contains_image(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in {"image_url", "input_image", "image"}:
                return True
            if _contains_image(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_image(item) for item in value)
    return False


def estimate_text_tokens(value: object) -> int | None:
    """Coarse display-only estimate. Never written into measured token columns."""
    text = " ".join(_texts(value)).strip()
    if not text:
        return None
    cjk = len(_CJK.findall(text))
    latin = len(_LATIN_WORD.findall(text))
    residual = max(0, len(text) - cjk - sum(len(x) for x in _LATIN_WORD.findall(text)))
    return max(1, math.ceil(cjk * 1.15 + latin * 1.3 + residual * 0.18))


def _set_task_link(session, module: str, task_id: str, title: str | None = None,
                   workspace_id: str | None = None, *, allow_unlinked: bool = True):
    link = session.scalar(select(UsageTaskLink).where(
        UsageTaskLink.module == module, UsageTaskLink.task_id == str(task_id)))
    if link:
        if title and not link.title:
            link.title = str(title)[:240]
        if workspace_id and not link.workspace_id:
            link.workspace_id = str(workspace_id)
        return link
    inferred_title, inferred_workspace = infer_task_project(session, module, str(task_id))
    resolved_workspace = workspace_id or inferred_workspace
    if not allow_unlinked and not resolved_workspace:
        return None
    link = UsageTaskLink(
        module=module, task_id=str(task_id),
        title=str(title or inferred_title or task_id)[:240],
        workspace_id=str(resolved_workspace) if resolved_workspace else None,
    )
    session.add(link)
    return link


def infer_task_project(session, module: str, task_id: str) -> tuple[str | None, str | None]:
    if module in {"tagging", "text_extraction", "vision"}:
        job = session.get(BatchJob, task_id)
        if job:
            dataset = session.get(Dataset, job.dataset_id)
            title = ((job.spec or {}).get("title") or (job.spec or {}).get("kind") or "打标任务")
            return str(title)[:240], dataset.workspace_id if dataset else None
        if module == "vision":
            dataset = session.get(Dataset, task_id)
            if dataset:
                return str(dataset.source_filename or "视觉分析任务")[:240], dataset.workspace_id
    if module == "agent":
        dataset = session.get(Dataset, task_id)
        if dataset:
            return str(dataset.source_filename or "Reasonix Agent 会话")[:240], dataset.workspace_id
    if module == "workbench_mcp":
        run = session.get(BatchJob, task_id)
        if run:
            dataset = session.get(Dataset, run.dataset_id)
            return str((run.spec or {}).get("title") or "工作台 MCP 任务")[:240], dataset.workspace_id if dataset else None
        analysis = session.get(Run, task_id)
        if analysis:
            dataset = session.get(Dataset, analysis.dataset_id)
            title = (dataset.source_filename if dataset else None) or analysis.kind or "工作台 MCP 任务"
            return str(title)[:240], dataset.workspace_id if dataset else None
        dataset = session.get(Dataset, task_id)
        if dataset:
            return str(dataset.source_filename or "工作台 MCP 任务")[:240], dataset.workspace_id
    if module == "keyword_research":
        try:
            from .keyword_research.registry import read
            state = read(task_id)
            if state:
                return state.get("display_name") or "关键词调研任务", state.get("workspace_id")
        except Exception:
            pass
    if module == "amazon_reviews":
        try:
            from .amazon_reviews import service as reviews
            state = reviews._read(task_id)
            if state:
                return state.get("display_name") or "Amazon 评论任务", state.get("workspace_id")
        except Exception:
            pass
    if module == "amazon_voc":
        try:
            from .amazon_voc import service as voc
            state = voc.service.get_run(task_id)
            if state:
                title = state.get("name") or state.get("project_name") or "Amazon VOC 任务"
                return title, state.get("workspace_id")
        except Exception:
            pass
    if module == "xunlongjue":
        try:
            from .xunlongjue.service import get_run
            state = get_run(task_id)
            if state:
                return state.get("name") or state.get("marketplace") or "寻龙诀任务", state.get("workspace_id")
        except Exception:
            pass
    return None, None


def start_usage_event(*, kind: str, module: str, task_id: str | None = None,
                      task_title: str | None = None, model: str | None = None,
                      provider: str | None = None, function_name: str | None = None,
                      server_name: str | None = None, attempt: int = 1,
                      usage_source: str | None = None, metadata: dict | None = None,
                      request_text: object = None) -> str | None:
    """Commit an in-progress row at the outbound request boundary."""
    try:
        event_id = uid()
        details = _safe_metadata(metadata or {})
        if kind == "model":
            details["estimated_request_text_tokens"] = estimate_text_tokens(request_text)
            details["image_tokens_unknown"] = _contains_image(request_text) if request_text is not None else False

        def insert(*, create_task_link: bool):
            with Session.begin() as session:
                if task_id and create_task_link:
                    _set_task_link(session, module, str(task_id), task_title)
                session.add(UsageEvent(
                    id=event_id, occurred_at=now(), instance_id=instance_identity(),
                    environment=environment_name(), kind=kind, module=module,
                    task_id=str(task_id) if task_id else None,
                    task_title=str(task_title or "")[:240] or None,
                    provider=provider or provider_name(model), model=str(model)[:240] if model else None,
                    function_name=str(function_name)[:160] if function_name else None,
                    server_name=str(server_name)[:120] if server_name else None,
                    status="in_progress", attempt=max(1, int(attempt or 1)),
                    usage_quality="unknown" if kind in {"model", "agent_turn"} else "not_applicable",
                    usage_source=usage_source, metadata_json=details,
                ))

        try:
            insert(create_task_link=True)
        except (IntegrityError, OperationalError):
            # Parallel first calls can race while creating the same task link.
            # Keep the per-attempt event even if this request cannot create that link.
            insert(create_task_link=False)
        return event_id
    except Exception:
        # Usage instrumentation must not interrupt a business request.
        return None


def finish_usage_event(event_id: str | None, *, status: str,
                       duration_ms: int | None = None, usage: object = None,
                       request_text: object = None, response_text: object = None,
                       usage_source: str | None = None, metadata: dict | None = None) -> None:
    if not event_id:
        return
    try:
        normalized = normalize_provider_usage(usage)
        with Session.begin() as session:
            row = session.get(UsageEvent, event_id)
            if not row:
                return
            status = str(status or "unknown")
            if status in {"http_error", "network_error", "error", "exception"}:
                status = "failed"
            row.status = status[:32]
            row.duration_ms = max(0, int(duration_ms)) if duration_ms is not None else None
            row.usage_source = usage_source or row.usage_source
            row.metadata_json = _safe_metadata({**(row.metadata_json or {}), **(metadata or {})})
            if row.kind == "agent_turn":
                for field in ("input_tokens", "output_tokens", "cache_hit_tokens",
                              "cache_miss_tokens", "cache_creation_tokens"):
                    if field in normalized:
                        setattr(row, field, normalized[field])
                measured = "input_tokens" in normalized or "output_tokens" in normalized
                row.usage_quality = "measured" if measured else "unknown"
                row.usage_source = usage_source or (
                    "agent_reported_aggregate" if measured else "agent_provider_usage_unavailable")
                return
            if row.kind != "model":
                row.usage_quality = "not_applicable"
                return
            for field in ("input_tokens", "output_tokens", "cache_hit_tokens", "cache_miss_tokens", "cache_creation_tokens"):
                if field in normalized:
                    setattr(row, field, normalized[field])
            if "input_tokens" in normalized or "output_tokens" in normalized:
                row.usage_quality = "measured"
                row.usage_source = usage_source or "provider_response"
            elif status in {"success", "completed"} and request_text is not None:
                row.estimated_input_tokens = estimate_text_tokens(request_text)
                row.estimated_output_tokens = estimate_text_tokens(response_text)
                if response_text is None:
                    row.estimated_output_tokens = 0
                row.usage_quality = "estimated"
                row.usage_source = usage_source or "text_only_estimate"
            elif status == "invalid" and response_text is not None:
                row.estimated_input_tokens = estimate_text_tokens(request_text)
                row.estimated_output_tokens = estimate_text_tokens(response_text)
                row.usage_quality = "estimated"
                row.usage_source = usage_source or "text_only_estimate"
            else:
                row.usage_quality = "unknown"
                row.usage_source = usage_source or "provider_usage_missing"
    except Exception:
        return


def record_local_cache(*, module: str, task_id: str | None, cache_name: str,
                       hit: bool, task_title: str | None = None) -> None:
    event_id = start_usage_event(
        kind="local_cache", module=module, task_id=task_id, task_title=task_title,
        function_name=cache_name, metadata={"cache_scope": "workbench_result_cache"},
    )
    finish_usage_event(event_id, status="hit" if hit else "miss", metadata={
        "cache_scope": "workbench_result_cache", "cache_name": cache_name,
    })


def _parse_range(params: dict) -> tuple[date, date, datetime, datetime, str, str]:
    tz = configured_timezone()
    today = datetime.now(tz).date()
    mode = str(params.get("range") or "last30")
    if mode == "custom":
        try:
            start_day = date.fromisoformat(str(params.get("start") or ""))
            end_day = date.fromisoformat(str(params.get("end") or ""))
        except ValueError:
            raise ValueError("自定义日期需要有效的开始和结束日期") from None
        if end_day < start_day:
            raise ValueError("结束日期不能早于开始日期")
    elif mode == "today":
        start_day = end_day = today
    elif mode == "this_week":
        start_day = today - timedelta(days=today.weekday())
        end_day = start_day + timedelta(days=6)
    elif mode == "this_month":
        start_day = today.replace(day=1)
        next_month = date(today.year + (today.month == 12), 1 if today.month == 12 else today.month + 1, 1)
        end_day = next_month - timedelta(days=1)
    elif mode == "last30":
        end_day = today
        start_day = today - timedelta(days=29)
    else:
        raise ValueError("时间范围仅支持今天、本周、本月、最近 30 天或自定义")
    start_dt = datetime.combine(start_day, time.min, tzinfo=tz).astimezone(timezone.utc)
    end_dt = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)
    return start_day, end_day, start_dt, end_dt, tz.key, mode


def _base_statement(params: dict):
    start_day, end_day, start_dt, end_dt, tz_name, mode = _parse_range(params)
    statement = (select(UsageEvent, UsageTaskLink, Workspace.name)
        .select_from(UsageEvent)
        .outerjoin(UsageTaskLink, and_(
            UsageTaskLink.module == UsageEvent.module,
            UsageTaskLink.task_id == UsageEvent.task_id,
        ))
        .outerjoin(Workspace, Workspace.id == UsageTaskLink.workspace_id)
        .where(UsageEvent.occurred_at >= start_dt.isoformat(),
               UsageEvent.occurred_at < end_dt.isoformat(),
               UsageEvent.instance_id == instance_identity()))
    mapping = {
        "workspace_id": UsageTaskLink.workspace_id,
        "task_id": UsageEvent.task_id,
        "module": UsageEvent.module,
        "provider": UsageEvent.provider,
        "model": UsageEvent.model,
        "function_name": UsageEvent.function_name,
        "quality": UsageEvent.usage_quality,
    }
    for key, column in mapping.items():
        value = str(params.get(key) or "").strip()
        if value:
            statement = statement.where(column == value)
    return statement, (start_day, end_day, tz_name, mode)


def _event_dict(event: UsageEvent, link: UsageTaskLink | None, project_name: str | None) -> dict:
    return {
        "id": event.id, "occurred_at": event.occurred_at, "kind": event.kind,
        "module": event.module, "task_id": event.task_id,
        "environment": event.environment,
        "task_title": (link.title if link and link.title else event.task_title),
        "workspace_id": link.workspace_id if link else None,
        "project_name": project_name or ("未关联项目" if not (link and link.workspace_id) else None),
        "provider": event.provider, "model": event.model,
        "function_name": event.function_name, "server_name": event.server_name,
        "status": event.status, "attempt": event.attempt, "duration_ms": event.duration_ms,
        "input_tokens": event.input_tokens, "output_tokens": event.output_tokens,
        "cache_hit_tokens": event.cache_hit_tokens,
        "cache_miss_tokens": event.cache_miss_tokens,
        "cache_creation_tokens": event.cache_creation_tokens,
        "estimated_input_tokens": event.estimated_input_tokens,
        "estimated_output_tokens": event.estimated_output_tokens,
        "usage_quality": event.usage_quality, "usage_source": event.usage_source,
        "metadata": event.metadata_json or {},
    }


def _period_key(value: str, bucket: str, timezone_name: str) -> str:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local_day = moment.astimezone(ZoneInfo(timezone_name)).date()
    if bucket == "week":
        monday = local_day - timedelta(days=local_day.weekday())
        return monday.isoformat()
    if bucket == "month":
        return local_day.strftime("%Y-%m")
    return local_day.isoformat()


def _empty_aggregate() -> dict:
    return {"model_calls": 0, "mcp_calls": 0, "pending_calls": 0, "input_tokens": 0,
            "output_tokens": 0, "estimated_input_tokens": 0,
            "estimated_output_tokens": 0, "cache_hit_tokens": 0,
            "cache_miss_tokens": 0, "cache_creation_tokens": 0,
            "measured_calls": 0, "estimated_calls": 0, "unknown_calls": 0,
            "agent_turns": 0,
            "mcp_successes": 0, "mcp_failures": 0,
            "local_result_cache_hits": 0, "local_result_cache_misses": 0}


def _add_event(aggregate: dict, event: UsageEvent) -> None:
    if event.status == "in_progress":
        aggregate["pending_calls"] += 1
        return
    if event.kind == "model":
        aggregate["model_calls"] += 1
        aggregate["input_tokens"] += event.input_tokens or 0
        aggregate["output_tokens"] += event.output_tokens or 0
        aggregate["estimated_input_tokens"] += event.estimated_input_tokens or 0
        aggregate["estimated_output_tokens"] += event.estimated_output_tokens or 0
        aggregate["cache_hit_tokens"] += event.cache_hit_tokens or 0
        aggregate["cache_miss_tokens"] += event.cache_miss_tokens or 0
        aggregate["cache_creation_tokens"] += event.cache_creation_tokens or 0
        if event.usage_quality == "measured":
            aggregate["measured_calls"] += 1
        elif event.usage_quality == "estimated":
            aggregate["estimated_calls"] += 1
        else:
            aggregate["unknown_calls"] += 1
    elif event.kind == "mcp":
        aggregate["mcp_calls"] += 1
        if event.status == "success":
            aggregate["mcp_successes"] += 1
        elif event.status == "failed":
            aggregate["mcp_failures"] += 1
    elif event.kind == "agent_turn":
        aggregate["agent_turns"] += 1
        aggregate["input_tokens"] += event.input_tokens or 0
        aggregate["output_tokens"] += event.output_tokens or 0
        aggregate["cache_hit_tokens"] += event.cache_hit_tokens or 0
        aggregate["cache_miss_tokens"] += event.cache_miss_tokens or 0
        aggregate["cache_creation_tokens"] += event.cache_creation_tokens or 0
    elif event.kind == "local_cache":
        if event.status == "hit":
            aggregate["local_result_cache_hits"] += 1
        elif event.status == "miss":
            aggregate["local_result_cache_misses"] += 1


def summary(params: dict | None = None) -> dict:
    params = params or {}
    statement, (start_day, end_day, tz_name, mode) = _base_statement(params)
    bucket = str(params.get("bucket") or "day")
    if bucket not in {"day", "week", "month"}:
        raise ValueError("统计粒度仅支持日、周、月")
    with Session() as session:
        rows = session.execute(statement.order_by(UsageEvent.occurred_at.asc())).all()
        events = [(event, link, project_name) for event, link, project_name in rows]
        totals = _empty_aggregate()
        series, projects, tasks, models, functions = {}, {}, {}, {}, {}
        modules, providers, qualities = set(), set(), set()
        for event, link, project_name in events:
            _add_event(totals, event)
            modules.add(event.module)
            if event.provider: providers.add(event.provider)
            qualities.add(event.usage_quality)
            period = _period_key(event.occurred_at, bucket, tz_name)
            _add_event(series.setdefault(period, {"period": period, **_empty_aggregate()}), event)
            project_id = link.workspace_id if link and link.workspace_id else None
            project_key = project_id or "unlinked"
            project_row = projects.setdefault(project_key, {
                "workspace_id": project_id,
                "workspace_name": project_name or "未关联项目",
                "model_calls": 0, "mcp_calls": 0, "input_tokens": 0,
                "output_tokens": 0, "estimated_input_tokens": 0,
                "estimated_output_tokens": 0, "unknown_calls": 0,
            })
            _add_group_event(project_row, event)
            task_key = (event.module, event.task_id or "")
            task_row = tasks.setdefault(task_key, {
                "module": event.module, "task_id": event.task_id,
                "title": (link.title if link and link.title else event.task_title) or event.task_id or "未关联任务",
                "workspace_id": project_id,
                "workspace_name": project_name or "未关联项目",
                "model_calls": 0, "mcp_calls": 0, "input_tokens": 0,
                "output_tokens": 0, "estimated_input_tokens": 0,
                "estimated_output_tokens": 0, "unknown_calls": 0,
            })
            _add_group_event(task_row, event)
            if event.kind == "model":
                key = (event.provider or "未知", event.model or "未知")
                _add_group_event(models.setdefault(key, {
                    "provider": key[0], "model": key[1], "model_calls": 0,
                    "input_tokens": 0, "output_tokens": 0,
                    "estimated_input_tokens": 0, "estimated_output_tokens": 0,
                    "unknown_calls": 0,
                }), event)
            if event.kind == "mcp":
                key = (event.server_name or "MCP", event.function_name or "未知")
                function_row = functions.setdefault(key, {
                    "server_name": key[0], "function_name": key[1],
                    "mcp_calls": 0, "successes": 0, "failures": 0,
                })
                function_row["mcp_calls"] += 1
                function_row["successes"] += int(event.status == "success")
                function_row["failures"] += int(event.status == "failed")

        if bucket == "day":
            cursor = start_day
            while cursor <= end_day:
                key = cursor.isoformat()
                series.setdefault(key, {"period": key, **_empty_aggregate()})
                cursor += timedelta(days=1)
        elif bucket == "week":
            cursor = start_day - timedelta(days=start_day.weekday())
            last = end_day - timedelta(days=end_day.weekday())
            while cursor <= last:
                key = cursor.isoformat()
                series.setdefault(key, {"period": key, **_empty_aggregate()})
                cursor += timedelta(days=7)
        else:
            cursor = date(start_day.year, start_day.month, 1)
            last = date(end_day.year, end_day.month, 1)
            while cursor <= last:
                key = cursor.strftime("%Y-%m")
                series.setdefault(key, {"period": key, **_empty_aggregate()})
                cursor = date(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1)

        earliest = session.scalar(select(func.min(UsageEvent.occurred_at)).where(
            UsageEvent.instance_id == instance_identity()))
        history = {
            "earliest_event_at": earliest,
            "coverage": "recorded_from_instrumentation",
            "message": "仅显示当前实例从接入计量后记录的事件；旧任务不会自动补成用量，历史缺口不按零补齐。",
            "limitations": [
                "Reasonix Agent 回合数单独统计；ACP 未提供可核验的服务商 Token，所以 Agent Token 不计入实测或估算合计。",
                "图像输入 Token 若服务商未返回用量，保持未知；文本估算不会替代实测值。",
            ],
        }
    model_options = sorted({event.model for event, _, _ in events if event.model})
    function_options = sorted({event.function_name for event, _, _ in events if event.function_name})
    return {
        "timezone": tz_name,
        "environment": environment_name(),
        "range": {"start": start_day.isoformat(), "end": end_day.isoformat(), "mode": mode},
        "bucket": bucket,
        "totals": totals,
        "series": [series[key] for key in sorted(series)],
        "by_project": sorted(projects.values(), key=lambda item: (item["workspace_name"], item["workspace_id"] or "")),
        "by_task": sorted(tasks.values(), key=lambda item: (item["workspace_name"], item["module"], item["title"])),
        "by_model": sorted(models.values(), key=lambda item: (item["provider"], item["model"])),
        "by_function": sorted(functions.values(), key=lambda item: (item["server_name"], item["function_name"])),
        "options": {"modules": sorted(modules), "providers": sorted(providers),
                    "models": model_options, "functions": function_options,
                    "qualities": sorted(qualities)},
        "history": history,
    }


def _add_group_event(target: dict, event: UsageEvent) -> None:
    if event.status == "in_progress":
        target["pending_calls"] = target.get("pending_calls", 0) + 1
        return
    target["model_calls"] = target.get("model_calls", 0) + int(event.kind == "model")
    target["mcp_calls"] = target.get("mcp_calls", 0) + int(event.kind == "mcp")
    target["input_tokens"] = target.get("input_tokens", 0) + (event.input_tokens or 0)
    target["output_tokens"] = target.get("output_tokens", 0) + (event.output_tokens or 0)
    target["estimated_input_tokens"] = target.get("estimated_input_tokens", 0) + (event.estimated_input_tokens or 0)
    target["estimated_output_tokens"] = target.get("estimated_output_tokens", 0) + (event.estimated_output_tokens or 0)
    target["unknown_calls"] = target.get("unknown_calls", 0) + int(event.kind == "model" and event.usage_quality == "unknown")
    target["local_result_cache_hits"] = target.get("local_result_cache_hits", 0) + int(
        event.kind == "local_cache" and event.status == "hit")
    target["local_result_cache_misses"] = target.get("local_result_cache_misses", 0) + int(
        event.kind == "local_cache" and event.status == "miss")
    target["agent_turns"] = target.get("agent_turns", 0) + int(event.kind == "agent_turn")


def events(params: dict | None = None) -> dict:
    params = params or {}
    statement, _ = _base_statement(params)
    try:
        limit = max(1, min(200, int(params.get("limit") or 50)))
        offset = max(0, int(params.get("offset") or 0))
    except (TypeError, ValueError):
        raise ValueError("分页参数无效") from None
    with Session() as session:
        count = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
        rows = session.execute(statement.order_by(UsageEvent.occurred_at.desc()).limit(limit).offset(offset)).all()
        items = [_event_dict(event, link, project_name) for event, link, project_name in rows]
    return {"items": items, "total": count, "limit": limit, "offset": offset}


def _validate_workspace(session, workspace_id: str | None):
    if workspace_id in (None, ""):
        return None
    workspace = session.get(Workspace, str(workspace_id))
    if not workspace:
        raise ValueError("项目不存在")
    return workspace.id


def set_task_project(module: str, task_id: str, workspace_id: str | None) -> dict:
    module, task_id = str(module or "").strip(), str(task_id or "").strip()
    if not module or not task_id:
        raise ValueError("模块和任务标识不能为空")
    if len(module) > 64 or len(task_id) > 160:
        raise ValueError("模块或任务标识过长")
    with Session.begin() as session:
        if not session.scalar(select(UsageEvent.id).where(
            UsageEvent.instance_id == instance_identity(),
            UsageEvent.module == module, UsageEvent.task_id == task_id).limit(1)):
            raise ValueError("没有找到该任务的用量记录")
        workspace_id = _validate_workspace(session, workspace_id)
        link = _set_task_link(session, module, task_id, workspace_id=workspace_id)
        link.workspace_id = workspace_id
        link.updated_at = now()
        title = link.title
    return {"module": module, "task_id": task_id, "title": title, "workspace_id": workspace_id}
