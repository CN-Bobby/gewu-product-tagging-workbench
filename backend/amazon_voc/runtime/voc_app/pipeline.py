from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .db import utcnow
from .prompts import PromptRegistry
from .provider import usage_scope
from .quality import FIELD_NAMESPACES, NAMESPACES, evaluate, iter_values
from .schemas import SCHEMAS


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PACKAGE_ROOT / "scripts"
TERMINAL = {"completed", "failed", "cancelled"}
REVIEW_EVENTS_PER_CALL = 20
MAPPING_EVENTS_PER_CALL = 50
ASSIGNMENT_EVENTS_PER_CALL = 100
JOURNEY_EVENTS_PER_CALL = 100
MAPPING_REPAIR_VALUES_PER_CALL = 400
MAX_DICTIONARY_MODEL_INPUT_BYTES = 192_000
MAX_SEGMENT_PROPOSAL_INPUT_BYTES = 96_000
MAX_JOURNEY_STAGE_PROPOSAL_INPUT_BYTES = 192_000
JOURNEY_STAGE_SAMPLES_PER_SEGMENT = 2


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _read(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _stable(prefix, *values):
    digest = hashlib.sha1("\x1f".join(str(v or "").strip().casefold() for v in values).encode("utf-8")).hexdigest()[:10]
    return "%s-%s" % (prefix, digest.upper())


def _normal_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _exact_quote_fragment(quote, source):
    """Return a verbatim contiguous source fragment when a model joined excerpts with ellipses."""
    quote = str(quote or "")
    source = str(source or "")
    if not quote or not source:
        return None
    if _normal_text(quote) in _normal_text(source):
        return quote
    fragments = re.split(r"(?:\.{2,}|…+|⋯+)", quote)
    fragments.extend(re.split(r"(?<=[.!?])\s+", quote))
    for fragment in sorted(fragments, key=len, reverse=True):
        candidate = fragment.strip(" \t\r\n\"'“”‘’")
        if len(candidate) < 16:
            continue
        pattern = re.escape(candidate)
        pattern = re.sub(r"\\\s+", r"\\s+", pattern)
        match = re.search(pattern, source, flags=re.IGNORECASE)
        if match:
            return match.group(0)
    return None


def _repair_event_quotes(events):
    unresolved = []
    repaired = 0
    for event in events:
        quote = event.get("evidence_quote")
        source = event.get("review_text")
        if not quote or _normal_text(quote) not in _normal_text(source):
            exact = _exact_quote_fragment(quote, source)
            if exact:
                event["evidence_quote"] = exact
                repaired += 1
            else:
                unresolved.append(event.get("event_id"))
    return repaired, unresolved


def _chunks(rows, size):
    for offset in range(0, len(rows), size):
        yield rows[offset:offset + size]


def _json_size_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _bounded_signal_text(value, limit):
    parts = []

    def visit(item):
        if isinstance(item, dict):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)
        elif item is not None and not isinstance(item, bool):
            text = re.sub(r"\s+", " ", str(item)).strip()
            if text:
                parts.append(text)

    visit(value)
    return "；".join(parts)[:max(0, int(limit))]


def build_journey_stage_proposal_input(events, segments,
                                       max_bytes=MAX_JOURNEY_STAGE_PROPOSAL_INPUT_BYTES,
                                       samples_per_segment=JOURNEY_STAGE_SAMPLES_PER_SEGMENT):
    """Build bounded, date-stratified comment signals for dynamic journey-stage discovery."""
    events = list(events or [])
    segments = list(segments or [])
    event_by_id = {event.get("event_id"): event for event in events if event.get("event_id")}
    samples_per_segment = max(1, int(samples_per_segment or 1))
    max_bytes = max(1024, int(max_bytes or MAX_JOURNEY_STAGE_PROPOSAL_INPUT_BYTES))

    def make_payload(sample_limit, text_limit):
        compact_segments = []
        sampled_count = 0
        for segment in segments:
            member_events = [event_by_id[event_id] for event_id in segment.get("member_event_ids") or []
                             if event_id in event_by_id]
            if not member_events:
                continue
            member_events.sort(key=lambda event: (str(event.get("review_date") or ""),
                                                   str(event.get("event_id") or "")))
            sample_count = min(sample_limit, len(member_events))
            if sample_count == 1:
                sample_indexes = [0]
            else:
                sample_indexes = sorted({round(index * (len(member_events) - 1) / (sample_count - 1))
                                         for index in range(sample_count)})
            signals = []
            for index in sample_indexes:
                event = member_events[index]
                signals.append({
                    "event_id": str(event.get("event_id") or ""),
                    "scenario": _bounded_signal_text(event.get("scenario"), text_limit),
                    "motivation": _bounded_signal_text(event.get("motivation"), text_limit),
                    "job": _bounded_signal_text(event.get("task"), text_limit),
                    "experience": _bounded_signal_text(event.get("experience"), text_limit),
                    "workaround": _bounded_signal_text(event.get("workaround"), text_limit),
                    "evidence_quote": _bounded_signal_text(event.get("evidence_quote"), text_limit),
                })
            sampled_count += len(signals)
            compact_segments.append({
                "segment_id": str(segment.get("segment_id") or ""),
                "name": _bounded_signal_text(segment.get("name"), text_limit),
                "persona_summary": _bounded_signal_text(segment.get("persona_summary"), text_limit * 2),
                "jtbd_statement": _bounded_signal_text(segment.get("jtbd_statement"), text_limit * 2),
                "member_event_count": len(member_events),
                "review_signals": signals,
            })
        payload = {
            "event_count": len(events),
            "sampling_method": "evenly_spaced_by_review_date_within_each_active_segment",
            "max_samples_per_segment": sample_limit,
            "segments": compact_segments,
        }
        return payload, sampled_count

    for sample_limit, text_limit in ((samples_per_segment, 112), (1, 80), (1, 48), (1, 24)):
        payload, sampled_count = make_payload(sample_limit, text_limit)
        if _json_size_bytes(payload) <= max_bytes:
            payload["sampling"] = {
                "sampled_event_count": sampled_count,
                "active_segment_count": len(payload["segments"]),
                "max_samples_per_segment": sample_limit,
                "text_limit_chars": text_limit,
            }
            if _json_size_bytes(payload) <= max_bytes:
                return payload
    raise ValueError("旅程阶段样本摘要超过安全输入上限")


def build_segment_proposal_batches(proposal_input, max_bytes=MAX_SEGMENT_PROPOSAL_INPUT_BYTES):
    """Split complete profiles into bounded requests without truncating evidence."""
    profiles = list(proposal_input.get("profiles") or [])
    if not profiles:
        return []
    fixed = {key: value for key, value in proposal_input.items() if key not in ("profiles", "coverage")}
    full_coverage = proposal_input.get("coverage") or {}
    profile_chunks = []
    current = []
    for profile in profiles:
        candidate = current + [profile]
        if current and _json_size_bytes({**fixed, "profiles": candidate}) > max(1, max_bytes - 1024):
            profile_chunks.append(current)
            current = []
        current.append(profile)
    if current:
        profile_chunks.append(current)

    result = []
    for index, chunk in enumerate(profile_chunks, 1):
        chunk_event_count = sum(int(profile.get("e") or 0) for profile in chunk)
        payload = {
            **fixed,
            "profiles": chunk,
            "coverage": {
                "event_count": chunk_event_count,
                "profile_count": len(chunk),
                "all_events_included": True,
                "review_count_is_not_additive_across_profiles": True,
                "full_event_count": int(full_coverage.get("event_count") or 0),
                "full_profile_count": int(full_coverage.get("profile_count") or len(profiles)),
                "proposal_batch_index": index,
                "proposal_batch_count": len(profile_chunks),
            },
        }
        result.append({"payload": payload, "profile_count": len(chunk),
                       "event_count": chunk_event_count, "input_bytes": _json_size_bytes(payload)})
    return result


def _profile_value(value):
    if isinstance(value, dict):
        return {key: _profile_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_profile_value(item) for item in value if str(item or "").strip()]
    return str(value or "").strip()


def build_segment_proposal_input(events, dictionary, minimum_formal_reviews):
    """Create a bounded evidence profile instead of sending full events and mappings."""
    field_groups = (
        ("audience", ("user", "relationship", "physical_constraints", "skill_level", "role_needs")),
        ("scenario", ("context", "place", "environmental_stressors", "social_emotional_context", "trigger")),
        ("motivation", ("explicit", "inferred")),
        ("task", ("core_job", "functional_job", "emotional_job", "social_job", "desired_outcomes")),
    )
    signal_fields = (
        ("体验对象", "experience", "object"), ("组件", "experience", "component"),
        ("属性", "experience", "attribute"), ("结果", "experience", "result"),
        ("痛点", "experience", "pain"), ("痒点", "experience", "itch"),
        ("目标结果", "task", "desired_outcomes"),
        ("用户自救动作", "workaround", "standardized_action"),
    )

    def at(event, group, field):
        value = (event.get(group) or {}).get(field, "")
        return _profile_value(value)

    def identity(value):
        if isinstance(value, dict):
            return {key: identity(item) for key, item in value.items()}
        if isinstance(value, list):
            return [identity(item) for item in value]
        return _normal_text(value)

    groups = {}
    for event in events:
        dimensions = [[at(event, group, field) for field in fields] for group, fields in field_groups]
        has_dimensions = any(_normal_text(item) for group in dimensions for value in group
                             for item in (value if isinstance(value, list) else [value]))
        fallback = None
        if not has_dimensions:
            fallback = [at(event, "experience", field)
                        for field in ("object", "component", "attribute", "result", "pain", "itch")]
            fallback.append(at(event, "workaround", "standardized_action"))
        key = json.dumps({"dimensions": identity(dimensions), "fallback": identity(fallback)},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        profile = groups.get(key)
        if profile is None:
            profile = {
                "dimensions": dimensions,
                "fallback": fallback,
                "event_count": 0,
                "_review_ids": set(),
                "_signals": {},
                "_examples": [],
            }
            groups[key] = profile
        profile["event_count"] += 1
        review_id = str(event.get("review_id") or "").strip()
        if review_id:
            profile["_review_ids"].add(review_id)
        event_id = str(event.get("event_id") or "").strip()
        for label, group, field in signal_fields:
            value = at(event, group, field)
            values = value if isinstance(value, list) else [value]
            for raw in values:
                normalized = _normal_text(raw)
                if normalized:
                    signal_key = (label, normalized)
                    entry = profile["_signals"].setdefault(signal_key, {"type": label, "value": raw, "event_count": 0})
                    entry["event_count"] += 1
        quote = str(event.get("evidence_quote") or "").strip()
        if quote and not profile["_examples"]:
            profile["_examples"].append([event_id, review_id,
                                         quote[:120]])

    profiles = []
    for profile in groups.values():
        row = {"d": profile["dimensions"], "e": profile["event_count"],
               "r": len(profile["_review_ids"]),
               "n": [[signal["type"], signal["value"], signal["event_count"]]
                     for signal in sorted(profile["_signals"].values(),
                                          key=lambda item: (-item["event_count"], item["type"], item["value"]))[:3]],
               "q": profile["_examples"]}
        if profile["fallback"]:
            row["f"] = profile["fallback"]
        profiles.append(row)
    profiles.sort(key=lambda row: (-row["r"], -row["e"], json.dumps(row["d"], ensure_ascii=False)))
    namespaces = {"audience", "scenario", "motivation", "task", "desired_outcome", "object", "component",
                  "attribute", "result", "pain", "itch", "workaround_action"}
    tag_catalog = [{key: tag.get(key, "") for key in ("namespace", "canonical_name", "definition")}
                   for tag in dictionary.get("tags") or []
                   if tag.get("namespace") in namespaces and tag.get("normalization_status") != "raw_unmerged"]
    return {
        "profiles": profiles,
        "tag_catalog": tag_catalog,
        "minimum_formal_reviews": minimum_formal_reviews,
        "coverage": {"event_count": len(events), "profile_count": len(profiles),
                     "all_events_included": True,
                     "review_count_is_not_additive_across_profiles": True},
    }


def _schema_errors(value, schema, path="$", limit=20):
    errors = []
    def walk(item, rule, current):
        if len(errors) >= limit:
            return
        if "anyOf" in rule:
            if not any(not _schema_errors(item, candidate, current, 1) for candidate in rule["anyOf"]):
                errors.append(current + " does not match anyOf")
            return
        kind = rule.get("type")
        valid = ((kind == "object" and isinstance(item, dict)) or
                 (kind == "array" and isinstance(item, list)) or
                 (kind == "string" and isinstance(item, str)) or
                 (kind == "integer" and isinstance(item, int) and not isinstance(item, bool)) or
                 (kind == "boolean" and isinstance(item, bool)) or
                 (kind == "null" and item is None) or kind is None)
        if not valid:
            errors.append("%s expected %s" % (current, kind)); return
        if "enum" in rule and item not in rule["enum"]:
            errors.append("%s invalid enum" % current)
        if isinstance(item, dict):
            for key in rule.get("required") or []:
                if key not in item:
                    errors.append("%s.%s missing" % (current, key))
            if rule.get("additionalProperties") is False:
                for key in set(item) - set(rule.get("properties") or {}):
                    errors.append("%s.%s unexpected" % (current, key))
            for key, child in (rule.get("properties") or {}).items():
                if key in item: walk(item[key], child, current + "." + key)
        if isinstance(item, list):
            if "maxItems" in rule and len(item) > rule["maxItems"]:
                errors.append("%s too many items" % current)
            for index, child in enumerate(item):
                walk(child, rule.get("items", {}), "%s[%s]" % (current, index))
    walk(value, schema, path)
    return errors


def _batches(rows, max_reviews, max_chars):
    batch, chars = [], 0
    for row in rows:
        size = len(str(row.get("content") or ""))
        if size > max_chars:
            raise ValueError("单条评论超过批次字符上限：%s > %s (%s)" %
                             (size, max_chars, row.get("review_id")))
        if batch and (len(batch) >= max_reviews or chars + size > max_chars):
            yield batch
            batch, chars = [], 0
        batch.append(row); chars += size
    if batch:
        yield batch


class Pipeline:
    def __init__(self, settings, db, storage, provider, prompts=None):
        self.settings = settings
        self.db = db
        self.storage = storage
        self.provider = provider
        self.prompts = prompts or PromptRegistry()

    def _status(self, run_id, status):
        run = self.db.get_run(run_id)
        if run and run.get("cancel_requested"):
            self.db.update_run(run_id, status="cancelled", completed_at=utcnow())
            raise InterruptedError("任务已取消")
        self.db.update_run(run_id, status=status, error="")

    @staticmethod
    def _coverage_round(workdir, task):
        rounds = _read(Path(workdir) / "cache" / "coverage_rounds.json", {}) or {}
        return int(rounds.get(task, 0))

    @staticmethod
    def _advance_coverage_round(workdir, task):
        path = Path(workdir) / "cache" / "coverage_rounds.json"
        rounds = _read(path, {}) or {}
        rounds[task] = int(rounds.get(task, 0)) + 1
        _write(path, rounds)
        return rounds[task]

    def _call(self, run, revision, workdir, task, batch_id, payload, reviewer=False, schema_override=None):
        prompt_meta, instructions = self.prompts.load(task)
        schema = schema_override or SCHEMAS[task]
        digest = _hash({"prompt_version": prompt_meta["version"], "payload": payload})
        existing = self.db.one("SELECT * FROM batches WHERE run_id=? AND revision=? AND stage=? AND batch_id=?",
                               (run["id"], revision, task, batch_id))
        if existing and existing["status"] == "completed" and existing["input_hash"] == digest:
            cached = Path(existing["output_path"])
            if cached.exists():
                return _read(cached)
        output_path = Path(workdir) / "cache" / "batches" / task / (batch_id + ".json")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        now = utcnow()
        self.db.execute("INSERT INTO batches(run_id,revision,stage,batch_id,input_hash,status,output_path,attempts,updated_at) "
                        "VALUES(?,?,?,?,?,'running',?,0,?) ON CONFLICT(run_id,revision,stage,batch_id) DO UPDATE SET "
                        "input_hash=excluded.input_hash,status='running',output_path=excluded.output_path,updated_at=excluded.updated_at",
                        (run["id"], revision, task, batch_id, digest, str(output_path), now))
        errors = []
        attempts = self.settings.max_repairs + 2
        attempts_made = 0
        retryable_error = True
        for attempt in range(attempts):
            attempts_made = attempt + 1
            model = self.settings.reviewer_model if reviewer or attempt == attempts - 1 else self.settings.extractor_model
            request_payload = payload if not errors else {"original_input": payload, "validation_errors": errors,
                                                           "repair_instruction": "修复结构并返回完整结果"}
            allowed_segment_ids = (((schema.get("properties") or {}).get("assignments") or {}).get("items") or {}).get("properties", {}).get("segment_id", {}).get("enum")
            if errors and allowed_segment_ids:
                request_payload["repair_instruction"] += "；segment_id 只能使用这些候选 ID：" + json.dumps(allowed_segment_ids, ensure_ascii=False)
            started = datetime.now(timezone.utc)
            try:
                context = {"run_id": run["id"],
                           "task_title": "Amazon VOC · " + str(run.get("project_name") or run.get("name") or "任务"),
                           "stage": task, "batch_id": batch_id,
                           "pipeline_attempt": attempt + 1}
                with usage_scope(context):
                    result, usage = self.provider.call(task, model, instructions, request_payload, schema)
                errors = _schema_errors(result, schema)
                status, error_text = ("completed", "") if not errors else ("invalid", "; ".join(errors[:8]))
                retryable_error = True
            except Exception as exc:
                result, usage, errors, status, error_text = None, {}, [str(exc)], "failed", str(exc)
                retryable_error = bool(getattr(exc, "retryable", True))
            latency = usage.get("latency_ms") if usage else None
            if latency is None:
                latency = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            self.db.execute("INSERT INTO model_calls(run_id,revision,stage,batch_id,input_hash,model,prompt_version,protocol,"
                            "latency_ms,input_tokens,output_tokens,retries,status,error,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (run["id"], revision, task, batch_id, digest, model, prompt_meta["version"],
                             self.settings.model_protocol, latency, usage.get("input_tokens"), usage.get("output_tokens"),
                             attempt, status, error_text[:2000], utcnow()))
            if result is not None and not errors:
                _write(output_path, result)
                self.db.execute("UPDATE batches SET status='completed',attempts=?,updated_at=? WHERE run_id=? AND revision=? AND stage=? AND batch_id=?",
                                (attempt + 1, utcnow(), run["id"], revision, task, batch_id))
                return result
            if result is None and not retryable_error:
                break
        self.db.execute("UPDATE batches SET status='failed',attempts=?,updated_at=? WHERE run_id=? AND revision=? AND stage=? AND batch_id=?",
                        (attempts_made, utcnow(), run["id"], revision, task, batch_id))
        raise ValueError("%s/%s 连续结构校验失败: %s" % (task, batch_id, "; ".join(errors[:8])))

    def _call_many(self, run, revision, workdir, task, jobs, reviewer=False):
        """Run independent model batches using the worker's configured bounded concurrency."""
        jobs = list(jobs)
        if not jobs:
            return []
        workers = max(1, min(int(getattr(self.settings, "concurrency", 1) or 1), len(jobs)))
        if workers == 1:
            return [self._call(run, revision, workdir, task, batch_id, payload, reviewer=reviewer)
                    for batch_id, payload in jobs]
        outputs = [None] * len(jobs)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._call, run, revision, workdir, task, batch_id, payload, reviewer): index
                       for index, (batch_id, payload) in enumerate(jobs)}
            for future in as_completed(futures):
                outputs[futures[future]] = future.result()
        return outputs

    def _export_calls(self, run, revision, workdir):
        rows = self.db.all("SELECT stage,batch_id,input_hash,model,prompt_version,protocol,latency_ms,input_tokens,"
                           "output_tokens,retries,status,error,created_at FROM model_calls WHERE run_id=? AND revision=? ORDER BY id",
                           (run["id"], revision))
        _write(Path(workdir) / "cache" / "model_calls.json", {"schema": "amazon-voc-model-calls/v1", "calls": rows})

    def _run_script(self, name, *args):
        env = os.environ.copy(); env["PYTHONUTF8"] = "1"
        result = subprocess.run([sys.executable, str(SCRIPTS / name), *map(str, args)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        if result.returncode:
            raise RuntimeError((result.stdout + "\n" + result.stderr).strip())
        return result.stdout

    def _initialize(self, run, workdir):
        sources = run["sources"]
        args = ["--reviews", sources["reviews"], "--workdir", workdir,
                "--marketplace", run["marketplace"], "--country", run["country"],
                "--language", run["language"], "--category", run["category"], "--task-id", run["id"]]
        self._run_script("load_input.py", *args)
        self._run_script("clean_data.py", "--workdir", workdir)

    def _extract(self, run, revision, workdir):
        cleaned = _read(Path(workdir) / "cache" / "cleaned_reviews.json", {"rows": []})["rows"]
        master = _read(Path(workdir) / "cache" / "master.json", {})
        batches = list(_batches(cleaned, self.settings.batch_reviews, self.settings.batch_chars))
        outputs = {}
        def execute(index, rows):
            payload = {"marketplace": master.get("marketplace"), "category": master.get("category"), "reviews": rows}
            return index, self._call(run, revision, workdir, "extract_events", "batch-%05d" % index, payload)
        with ThreadPoolExecutor(max_workers=self.settings.concurrency) as pool:
            futures = [pool.submit(execute, i, rows) for i, rows in enumerate(batches, 1)]
            for future in as_completed(futures):
                index, output = future.result(); outputs[index] = output
        events, processing = [], []
        review_by_id = {r["review_id"]: r for r in cleaned}
        for index in sorted(outputs):
            for review_result in outputs[index].get("reviews") or []:
                review_id = review_result["review_id"]
                source = review_by_id.get(review_id, {})
                rows = review_result.get("events") or []
                processing.append({"review_id": review_id, "status": review_result.get("status"),
                                   "reason": review_result.get("no_signal_reason", ""), "event_count": len(rows)})
                for ordinal, event in enumerate(rows, 1):
                    event = dict(event)
                    event["event_id"] = "%s-事%02d" % (review_id, ordinal)
                    event["review_id"] = review_id
                    event["asin"] = source.get("asin", "")
                    event["rating"] = source.get("rating", "")
                    event["review_date"] = source.get("review_date", "")
                    event["review_text"] = source.get("content", "")
                    events.append(event)
        metadata = {"model": self.settings.extractor_model, "model_version": self.settings.extractor_model,
                    "prompt_version": self.prompts.load("extract_events")[0]["version"], "generated_at": utcnow()}
        _write(Path(workdir) / "cache" / "events.json", {"schema": "amazon-voc-events/v4", "metadata": metadata, "events": events})
        _write(Path(workdir) / "cache" / "review_processing.json",
               {"schema": "amazon-voc-review-processing/v1", "reviews": processing})

    def _reconcile_extraction_coverage(self, run, revision, workdir):
        """Recover event-backed processing rows and re-extract omitted reviews only."""
        cache = Path(workdir) / "cache"
        cleaned = (_read(cache / "cleaned_reviews.json", {"rows": []}) or {}).get("rows") or []
        events_doc = _read(cache / "events.json", {"events": [], "metadata": {}}) or {}
        events = events_doc.get("events") or []
        processing_doc = _read(cache / "review_processing.json", {"reviews": []}) or {}
        processing = []
        processing_signatures = {}
        for row in processing_doc.get("reviews") or []:
            review_id = row.get("review_id")
            signature = (review_id, row.get("status"), row.get("reason"), row.get("event_count"))
            if review_id and signature in processing_signatures:
                # Identical duplicate rows are a recoverable cache artifact. Conflicting
                # rows remain visible to the quality gate for manual diagnosis.
                continue
            if review_id:
                processing_signatures[signature] = True
            processing.append(row)
        cleaned_by_id = {row.get("review_id"): row for row in cleaned if row.get("review_id")}
        event_counts = {}
        for event in events:
            review_id = event.get("review_id")
            if review_id in cleaned_by_id:
                event_counts[review_id] = event_counts.get(review_id, 0) + 1

        # If events survived a prior interruption but their processing row did not,
        # reconstruct that row from the event cache without making a model call.
        processing_by_id = {}
        for row in processing:
            review_id = row.get("review_id")
            if review_id and review_id not in processing_by_id:
                processing_by_id[review_id] = row
        for review in cleaned:
            review_id = review.get("review_id")
            if not review_id:
                continue
            row = processing_by_id.get(review_id)
            if event_counts.get(review_id) and (
                    not row or row.get("status") != "已生成事件" or
                    int(row.get("event_count") or 0) != event_counts[review_id]):
                recovered = {"review_id": review_id, "status": "已生成事件",
                             "reason": "由已有语义事件缓存恢复处理记录",
                             "event_count": event_counts[review_id]}
                if row:
                    row.update(recovered)
                else:
                    processing.append(recovered)
                    processing_by_id[review_id] = recovered

        def pending_reviews():
            rows = []
            for review in cleaned:
                review_id = review.get("review_id")
                processing_row = processing_by_id.get(review_id)
                if (not processing_row or processing_row.get("status") == "失败" or
                        (processing_row.get("status") == "已生成事件" and not event_counts.get(review_id))):
                    rows.append(review)
            return rows

        pending = pending_reviews()
        coverage_round = self._coverage_round(workdir, "extraction")
        for attempt in range(2):
            if not pending:
                break
            for batch_number, chunk in enumerate(_chunks(pending, max(1, int(self.settings.batch_reviews))), 1):
                requested_ids = {row["review_id"] for row in chunk}
                batch_id = "extract-coverage-r%02d-%05d-a%02d" % (coverage_round, batch_number, attempt + 1)
                payload = {"marketplace": (_read(cache / "master.json", {}) or {}).get("marketplace"),
                           "category": (_read(cache / "master.json", {}) or {}).get("category"),
                           "reviews": chunk,
                           "required_review_ids": sorted(requested_ids),
                           "selection_reason": "补齐已清洗评论的抽取处理记录；每个 required_review_id 必须返回一次明确结果。"}
                try:
                    result = self._call(run, revision, workdir, "extract_events", batch_id, payload)
                except Exception:
                    # Keep the unresolved reviews explicit below; a provider failure
                    # must never be recast as an evidence-free no-signal decision.
                    continue
                accepted = set()
                for review_result in result.get("reviews") or []:
                    review_id = review_result.get("review_id")
                    if review_id not in requested_ids or review_id in accepted:
                        continue
                    source = cleaned_by_id.get(review_id)
                    if not source:
                        continue
                    status = review_result.get("status")
                    rows = review_result.get("events") or []
                    if status == "已生成事件" and rows:
                        event_rows = []
                        for ordinal, event in enumerate(rows, 1):
                            item = dict(event)
                            item["event_id"] = "%s-事%02d" % (review_id, ordinal)
                            item["review_id"] = review_id
                            item["asin"] = source.get("asin", "")
                            item["rating"] = source.get("rating", "")
                            item["review_date"] = source.get("review_date", "")
                            item["review_text"] = source.get("content", "")
                            event_rows.append(item)
                        known_event_ids = {event.get("event_id") for event in events}
                        if any(event["event_id"] in known_event_ids for event in event_rows):
                            continue
                        events.extend(event_rows)
                        event_counts[review_id] = len(event_rows)
                        processing_row = {"review_id": review_id, "status": "已生成事件",
                                          "reason": "缺失评论的定向补抽完成",
                                          "event_count": len(event_rows)}
                    elif status == "无可用需求信号" and not rows and str(review_result.get("no_signal_reason") or "").strip():
                        processing_row = {"review_id": review_id, "status": "无可用需求信号",
                                          "reason": str(review_result.get("no_signal_reason"))[:500],
                                          "event_count": 0}
                        event_counts[review_id] = 0
                    else:
                        continue
                    if review_id in processing_by_id:
                        processing_by_id[review_id].update(processing_row)
                    else:
                        processing.append(processing_row)
                        processing_by_id[review_id] = processing_row
                    accepted.add(review_id)
            events_doc["events"] = events
            processing_doc["reviews"] = processing
            _write(cache / "events.json", events_doc)
            _write(cache / "review_processing.json", processing_doc)
            pending = pending_reviews()

        if pending:
            pending_ids = {row.get("review_id") for row in pending}
            for review in pending:
                review_id = review.get("review_id")
                failed = {"review_id": review_id, "status": "失败",
                          "reason": "抽取模型未返回可验证的处理结果，未将其视为无需求信号",
                          "event_count": event_counts.get(review_id, 0)}
                if review_id in processing_by_id:
                    processing_by_id[review_id].update(failed)
                else:
                    processing.append(failed)
                    processing_by_id[review_id] = failed
            processing_doc["reviews"] = processing
            _write(cache / "review_processing.json", processing_doc)
            self._advance_coverage_round(workdir, "extraction")
            raise ValueError("抽取覆盖仍缺少 %s 条评论的有效结果" % len(pending_ids))

        events_doc["events"] = events
        processing_doc["reviews"] = processing
        _write(cache / "events.json", events_doc)
        _write(cache / "review_processing.json", processing_doc)

    @staticmethod
    def _review_selection(events):
        selected = {e["event_id"] for e in events if e.get("confidence") == "低" or
                    ((e.get("motivation") or {}).get("inferred") and not (e.get("motivation") or {}).get("inference_basis")) or
                    not e.get("evidence_quote") or
                    _normal_text(e.get("evidence_quote")) not in _normal_text(e.get("review_text"))}
        strata = {}
        for event in events:
            strata.setdefault((event.get("asin"), event.get("rating")), []).append(event)
        for rows in strata.values():
            rows = sorted(rows, key=lambda e: _hash(e.get("event_id")))
            selected.update(e["event_id"] for e in rows[:max(1, int(math.ceil(len(rows) * .10)))])
        return selected

    @staticmethod
    def _review_cache_complete(workdir):
        cache = Path(workdir) / "cache"
        review = _read(cache / "semantic_review.json", {}) or {}
        selected = set(review.get("selected_event_ids") or [])
        decided = {row.get("event_id") for row in review.get("decisions") or []}
        return (bool(review.get("complete")) and selected <= decided and
                not review.get("missing_event_ids") and not review.get("unresolved_quote_event_ids"))

    def _review(self, run, revision, workdir):
        path = Path(workdir) / "cache" / "events.json"
        doc = _read(path); events = doc["events"]
        review_path = Path(workdir) / "cache" / "semantic_review.json"
        previous = _read(review_path, {}) or {}
        coverage_round = self._coverage_round(workdir, "review")
        dropped_unverifiable_event_ids = set(previous.get("dropped_unverifiable_event_ids") or [])
        event_review_ids = {event.get("event_id"): event.get("review_id") for event in events}
        _, unresolved_before = _repair_event_quotes(events)
        # After two focused reviewer rounds, do not keep spending calls on an event whose
        # quote still cannot be verified verbatim. Exclude it from all downstream claims.
        if coverage_round >= 2:
            dropped_unverifiable_event_ids.update(unresolved_before)
            events = [event for event in events if event.get("event_id") not in unresolved_before]
            doc["events"] = events
        selected = self._review_selection(events)
        review_rows = [e for e in events if e["event_id"] in selected]
        current_event_ids = {event["event_id"] for event in events}
        current_selection_rule = "低置信/无依据推断/无效原文证据 + ASIN×评分10%分层抽样；空缺的可选字段不单独触发复核"
        reuse_previous = str(previous.get("selection_rule", "")).startswith(
            "低置信/无依据推断/无效原文证据")
        decisions_by_id = ({row.get("event_id"): row for row in previous.get("decisions") or []
                            if row.get("event_id") in selected and row.get("event_id") in current_event_ids}
                           if reuse_previous else {})
        # Re-open unresolved quotes even if an earlier reviewer accepted them.
        for event_id in unresolved_before:
            decisions_by_id.pop(event_id, None)
        missing_event_ids = []
        call_chunks = []
        for batch_number, chunk in enumerate(_chunks(review_rows, REVIEW_EVENTS_PER_CALL), 1):
            pending = [event for event in chunk if event["event_id"] not in decisions_by_id]
            base_batch_id = "review-r%02d-%05d" % (coverage_round, batch_number)
            if pending:
                requested_ids = sorted(event["event_id"] for event in pending)
                payload = {"events": pending,
                           "selection_reason": "低置信/异常 + ASIN×评分10%分层抽样；每个 required_event_id 必须返回一次复核决定",
                           "required_event_ids": requested_ids}
                call_chunks.append((base_batch_id, pending, payload))
        base_results = self._call_many(run, revision, workdir, "review_events",
                                       [(batch_id, payload) for batch_id, _, payload in call_chunks], reviewer=True)
        for (base_batch_id, original_chunk, _), result in zip(call_chunks, base_results):
            pending = list(original_chunk)
            for attempt in range(3):
                if not pending:
                    break
                requested_ids = {event["event_id"] for event in pending}
                if attempt:
                    result = self._call(
                        run, revision, workdir, "review_events", base_batch_id + "-coverage-%02d" % attempt,
                        {"events": pending,
                         "selection_reason": "只补齐 required_event_ids 中遗漏的复核决定",
                         "required_event_ids": sorted(requested_ids)}, reviewer=True)
                for decision in result.get("decisions") or []:
                    event_id = decision.get("event_id")
                    if event_id in requested_ids and event_id not in decisions_by_id:
                        decisions_by_id[event_id] = decision
                pending = [event for event in pending if event["event_id"] not in decisions_by_id]
            missing_event_ids.extend(event["event_id"] for event in pending)

        decisions = [decisions_by_id[event["event_id"]] for event in review_rows
                     if event["event_id"] in decisions_by_id]
        review_doc = {
            "schema": "amazon-voc-semantic-review/v1",
            "selected_event_ids": sorted(selected),
            "selection_rule": current_selection_rule,
            "decisions": decisions,
            "missing_event_ids": sorted(set(missing_event_ids)),
            "unresolved_quote_event_ids": [],
            "complete": not missing_event_ids,
            "coverage_round": coverage_round,
            "review_model": self.settings.reviewer_model,
            "generated_at": utcnow(),
        }
        if missing_event_ids:
            _write(review_path, review_doc)
            self._advance_coverage_round(workdir, "review")
            raise ValueError("语义复核仍缺少 %s 个事件的决定" % len(set(missing_event_ids)))

        by_id = {e["event_id"]: e for e in events}
        for decision in decisions:
            event_id = decision.get("event_id")
            if event_id not in by_id:
                continue
            if decision.get("action") == "drop":
                by_id.pop(event_id, None)
            elif decision.get("action") == "replace" and decision.get("replacement"):
                before = by_id[event_id]
                replacement = dict(decision["replacement"])
                for key in ("event_id", "review_id", "asin", "rating", "review_date", "review_text"):
                    replacement[key] = before.get(key)
                by_id[event_id] = replacement
            else:
                by_id[event_id]["review_status"] = "reviewed"
        doc["events"] = list(by_id.values())
        repaired_after, unresolved_after = _repair_event_quotes(doc["events"])
        unresolved_after = sorted(set(unresolved_after))
        if unresolved_after and coverage_round >= 2:
            dropped_unverifiable_event_ids.update(unresolved_after)
            unresolved_ids = set(unresolved_after)
            doc["events"] = [event for event in doc["events"] if event.get("event_id") not in unresolved_ids]
            unresolved_after = []
        review_doc["unresolved_quote_event_ids"] = unresolved_after
        review_doc["complete"] = not missing_event_ids and not unresolved_after
        review_doc["dropped_unverifiable_event_ids"] = sorted(dropped_unverifiable_event_ids)
        review_doc["dropped_unverifiable_event_count"] = len(dropped_unverifiable_event_ids)
        review_doc["dropped_unverifiable_reason"] = (
            "引文经过两轮定向复核仍无法在原评论中精确定位，相关事件已排除"
            if dropped_unverifiable_event_ids else ""
        )
        if repaired_after:
            review_doc["evidence_quotes_repaired"] = repaired_after
        doc["metadata"]["review_model"] = self.settings.reviewer_model
        _write(path, doc)
        _write(review_path, review_doc)
        processing_path = Path(workdir) / "cache" / "review_processing.json"
        processing_doc = _read(processing_path)
        counts = {}
        for event in doc["events"]:
            counts[event["review_id"]] = counts.get(event["review_id"], 0) + 1
        for row in processing_doc["reviews"]:
            row["event_count"] = counts.get(row["review_id"], 0)
            if not row["event_count"] and row["status"] == "已生成事件":
                excluded_for_quote = any(
                    event_id in dropped_unverifiable_event_ids and event_review_ids.get(event_id) == row["review_id"]
                    for event_id in dropped_unverifiable_event_ids
                )
                reason = "引文无法精确定位到评论原文，相关事件已排除" if excluded_for_quote else "复核后无合格事件"
                row["status"], row["reason"] = "无可用需求信号", reason
        _write(processing_path, processing_doc)
        if unresolved_after:
            self._advance_coverage_round(workdir, "review")
            review_doc["coverage_round"] = self._coverage_round(workdir, "review")
            _write(review_path, review_doc)
            raise ValueError("有 %s 个事件的复核引用仍无法精确定位到评论原文" % len(unresolved_after))

    @staticmethod
    def _withhold_unbounded_inferences(workdir):
        cache = Path(workdir) / "cache"
        events_path = cache / "events.json"
        events_doc = _read(events_path, {}) or {}
        withheld = []
        for event in events_doc.get("events") or []:
            motivation = event.get("motivation") or {}
            if motivation.get("inferred") and not (motivation.get("inference_basis") or []):
                motivation["inferred"] = ""
                motivation["inference_basis"] = []
                event["motivation"] = motivation
                if event.get("event_id"):
                    withheld.append(event["event_id"])
        if not withheld:
            return 0
        _write(events_path, events_doc)
        review_path = cache / "semantic_review.json"
        review_doc = _read(review_path, {}) or {}
        prior = set(review_doc.get("unbounded_inference_withheld_event_ids") or [])
        prior.update(withheld)
        review_doc["unbounded_inference_withheld_event_ids"] = sorted(prior)
        review_doc["unbounded_inference_withheld_count"] = len(prior)
        review_doc["unbounded_inference_withheld_reason"] = (
            "复核后仍缺少可核验推理依据，已撤下推断字段；事件中有证据支持的其他内容保留。"
        )
        _write(review_path, review_doc)
        return len(withheld)

    def _normalize(self, run, revision, workdir):
        events = _read(Path(workdir) / "cache" / "events.json")["events"]
        master = _read(Path(workdir) / "cache" / "master.json")
        dictionary_path = Path(workdir) / "cache" / "tag_dictionary.json"
        prior_dictionary = _read(dictionary_path, {}) or {}
        dictionary_payload = {"events": events, "namespaces": NAMESPACES,
                              "marketplace": master.get("marketplace"), "category": master.get("category")}
        dictionary_input_bytes = _json_size_bytes(dictionary_payload)
        raw_passthrough_only = dictionary_input_bytes > MAX_DICTIONARY_MODEL_INPUT_BYTES
        result = ({"tags": []} if raw_passthrough_only else
                  self._call(run, revision, workdir, "build_dictionary", "dictionary", dictionary_payload))
        event_by_id = {e["event_id"]: e for e in events}
        tags = []
        for tag in result.get("tags") or []:
            tag = dict(tag)
            tag["tag_id"] = tag.get("tag_id") or _stable("TAG", tag.get("namespace"), tag.get("canonical_name"))
            tag["support_event_ids"] = sorted({eid for eid in tag.get("support_event_ids") or [] if eid in event_by_id})
            if not tag["support_event_ids"]:
                continue
            tag["normalization_status"] = "normalized"
            reviews = {event_by_id[eid]["review_id"] for eid in tag["support_event_ids"]}
            tag["independent_review_count"] = len(reviews)
            tag["status"] = "formal" if len(reviews) >= 2 else "exploratory"
            tags.append(tag)
        tag_by_id = {tag.get("tag_id"): tag for tag in tags}
        valid_mappings = {}
        repair_round = self._coverage_round(workdir, "mapping")
        prior_metadata = prior_dictionary.get("metadata") or {}
        prior_model_unmapped = int(prior_metadata.get(
            "mapping_model_unmapped_count", prior_metadata.get("mapping_missing_count", 0)) or 0)
        expected_mapping_keys = set()
        expected_mapping_rows = {}
        mapping_chunks = []
        for batch_number, chunk in enumerate(_chunks(events, MAPPING_EVENTS_PER_CALL), 1):
            compact_events = []
            required_keys = set()
            used_namespaces = set()
            for event in chunk:
                values = [{"field_path": path, "raw_value": raw, "namespace": namespace}
                          for path, raw, namespace in iter_values(event)]
                compact_events.append({"event_id": event["event_id"], "values": values})
                required_keys.update((event["event_id"], row["field_path"], _normal_text(row["raw_value"]))
                                     for row in values)
                for row in values:
                    key = (event["event_id"], row["field_path"], _normal_text(row["raw_value"]))
                    expected_mapping_rows[key] = {"event_id": event["event_id"], **row}
                used_namespaces.update(row["namespace"] for row in values)
            compact_tags = [{key: tag.get(key, "") for key in ("tag_id", "namespace", "canonical_name", "definition")}
                            for tag in tags if tag.get("namespace") in used_namespaces]
            expected_mapping_keys.update(required_keys)
            if not raw_passthrough_only:
                payload = {"events": compact_events, "tag_dictionary": compact_tags,
                           "field_namespace_map": FIELD_NAMESPACES}
                batch_prefix = "mapping-r00-%05d" % batch_number
                mapping_chunks.append((batch_prefix, compact_events, compact_tags, required_keys, payload))
        base_results = (self._call_many(run, revision, workdir, "map_dictionary",
                                        [(batch_id, payload) for batch_id, _, _, _, payload in mapping_chunks])
                        if mapping_chunks else [])

        def accept_valid(rows, allowed_keys):
            for row in rows:
                key = (row.get("event_id"), row.get("field_path"), _normal_text(row.get("raw_value")))
                tag = tag_by_id.get(row.get("tag_id"))
                expected_namespace = FIELD_NAMESPACES.get(row.get("field_path"))
                if key in allowed_keys and tag and tag.get("namespace") == expected_namespace:
                    valid_mappings.setdefault(key, {**row, "mapping_method": "model_normalized"})

        for (batch_prefix, compact_events, compact_tags, required_keys, _), result in zip(mapping_chunks, base_results):
            accept_valid(result.get("mappings") or [], required_keys)

        missing_keys = expected_mapping_keys - set(valid_mappings)
        # A previous run may already have exhausted both focused mapping repairs.
        # Reusing its dictionary should not charge for the same unsuccessful requests;
        # exact-value passthrough below preserves coverage transparently.
        repair_attempts = 0 if prior_model_unmapped or raw_passthrough_only else 2
        for repair_number in range(1, repair_attempts + 1):
            if not missing_keys:
                break
            missing_rows = [expected_mapping_rows[key] for key in sorted(missing_keys)]
            repair_jobs = []
            for batch_number, rows in enumerate(_chunks(missing_rows, MAPPING_REPAIR_VALUES_PER_CALL), 1):
                rows_by_event = {}
                used_namespaces = set()
                for row in rows:
                    rows_by_event.setdefault(row["event_id"], []).append(
                        {key: row[key] for key in ("field_path", "raw_value", "namespace")})
                    used_namespaces.add(row["namespace"])
                retry_events = [{"event_id": event_id, "values": values}
                                for event_id, values in sorted(rows_by_event.items())]
                compact_tags = [{key: tag.get(key, "") for key in
                                 ("tag_id", "namespace", "canonical_name", "definition", "raw_variants")}
                                for tag in tags if tag.get("namespace") in used_namespaces]
                repair_payload = {
                    "events": retry_events,
                    "tag_dictionary": compact_tags,
                    "field_namespace_map": FIELD_NAMESPACES,
                    "required_mappings": rows,
                    "selection_reason": "仅修复缺少有效tag_id或标签命名空间与字段不匹配的映射；每项必须引用所给词典中同命名空间的tag_id。",
                }
                batch_id = "mapping-r%02d-validation-%02d-%05d" % (repair_round, repair_number, batch_number)
                repair_jobs.append((batch_id, set((row["event_id"], row["field_path"],
                                                   _normal_text(row["raw_value"])) for row in rows), repair_payload))
            repair_results = self._call_many(run, revision, workdir, "map_dictionary",
                                             [(batch_id, payload) for batch_id, _, payload in repair_jobs])
            for (_, allowed_keys, _), result in zip(repair_jobs, repair_results):
                accept_valid(result.get("mappings") or [], allowed_keys)
            missing_keys = expected_mapping_keys - set(valid_mappings)
        mappings = [valid_mappings[key] for key in sorted(valid_mappings)]
        missing_mapping_count = len(expected_mapping_keys - set(valid_mappings))
        raw_fallback_count = 0
        if missing_mapping_count:
            # Preserve uncategorized model output faithfully. Exact raw-value tags
            # complete the evidence mapping but remain visibly distinct from
            # model-normalized vocabulary and are excluded from segment proposals.
            missing_rows = [expected_mapping_rows[key] for key in sorted(expected_mapping_keys - set(valid_mappings))]
            fallback_groups = {}
            for row in missing_rows:
                group_key = (row["namespace"], _normal_text(row["raw_value"]))
                group = fallback_groups.setdefault(group_key, {"raw_values": [], "event_ids": set(), "rows": []})
                if row["raw_value"] not in group["raw_values"]:
                    group["raw_values"].append(row["raw_value"])
                group["event_ids"].add(row["event_id"])
                group["rows"].append(row)
            used_tag_ids = {tag.get("tag_id") for tag in tags}
            for (namespace, normalized_raw), group in sorted(fallback_groups.items()):
                raw_value = sorted(group["raw_values"], key=lambda value: (len(value), value))[0]
                tag_id = _stable("RAW", namespace, normalized_raw)
                if tag_id in used_tag_ids:
                    tag_id = _stable("RAW", namespace, normalized_raw, "passthrough")
                used_tag_ids.add(tag_id)
                support_ids = sorted(group["event_ids"])
                review_count = len({event_by_id[event_id]["review_id"] for event_id in support_ids})
                tags.append({"tag_id": tag_id, "namespace": namespace, "canonical_name": raw_value,
                             "definition": "原始字段值透传，未执行同义词归并；不代表已确认的标准术语。",
                             "parent_tag_id": "", "raw_variants": sorted(group["raw_values"]),
                             "include_examples": [], "exclude_examples": [],
                             "support_event_ids": support_ids, "independent_review_count": review_count,
                             "confidence": "原始表达透传", "status": "exploratory",
                             "normalization_status": "raw_unmerged"})
                for row in group["rows"]:
                    key = (row["event_id"], row["field_path"], _normal_text(row["raw_value"]))
                    valid_mappings[key] = {"event_id": row["event_id"], "field_path": row["field_path"],
                                           "raw_value": row["raw_value"], "tag_id": tag_id,
                                           "mapping_method": "raw_passthrough"}
                    raw_fallback_count += 1
            mappings = [valid_mappings[key] for key in sorted(valid_mappings)]
        unresolved_mapping_count = len(expected_mapping_keys - set(valid_mappings))
        if unresolved_mapping_count:
            self._advance_coverage_round(workdir, "mapping")
        _write(dictionary_path,
               {"schema": "amazon-voc-tag-dictionary/v1", "metadata": {"prompt_version": self.prompts.load("build_dictionary")[0]["version"],
                  "model": self.settings.extractor_model, "generated_at": (prior_dictionary.get("metadata") or {}).get("generated_at") or utcnow(),
                  "mapping_coverage_round": repair_round, "mapping_missing_count": unresolved_mapping_count,
                  "mapping_model_unmapped_count": missing_mapping_count,
                  "normalized_tag_count": sum(tag.get("normalization_status") != "raw_unmerged" for tag in tags),
                  "raw_unmerged_tag_count": sum(tag.get("normalization_status") == "raw_unmerged" for tag in tags),
                  "raw_passthrough_mapping_count": raw_fallback_count,
                  "dictionary_generation_mode": ("raw_passthrough_input_too_large" if raw_passthrough_only
                                                 else "model_normalized"),
                  "dictionary_input_bytes": dictionary_input_bytes}, "tags": tags,
                  "mappings": mappings})

    def _segment(self, run, revision, workdir):
        cache = Path(workdir) / "cache"
        events = _read(cache / "events.json")["events"]
        dictionary = _read(cache / "tag_dictionary.json")
        cleaned = _read(cache / "cleaned_reviews.json")["rows"]
        master = _read(cache / "master.json")
        config = master.get("analysis_config") or {}
        minimum = max(int(config.get("segment_min_review_count", 2)),
                      int(math.ceil(len(cleaned) * float(config.get("segment_min_review_rate", .005)))))
        proposal_input = build_segment_proposal_input(events, dictionary, minimum)
        proposal_batches = build_segment_proposal_batches(proposal_input)
        proposal_results = self._call_many(
            run, revision, workdir, "propose_segments",
            [("segments" if len(proposal_batches) == 1 else "segments-b%04d" % index, batch["payload"])
             for index, batch in enumerate(proposal_batches, 1)])
        segments = []
        seen_segment_keys = set()
        for proposals in proposal_results:
            for proposed in proposals.get("segments") or []:
                segment = dict(proposed)
                segment_key = segment.get("segment_key") or {}
                stable_key = tuple(_normal_text(segment_key.get(key))
                                   for key in ("audience", "scenario", "motivation", "task"))
                if not any(stable_key) or stable_key in seen_segment_keys:
                    continue
                seen_segment_keys.add(stable_key)
                segments.append(segment)
        if not segments:
            raise ValueError("细分建议没有返回可用的人群、场景、动机或任务候选")
        used = set()
        for segment in segments:
            segment["segment_id"] = segment.get("segment_id") or _stable("SEG", *(segment.get("segment_key") or {}).values())
            if segment["segment_id"] in used:
                segment["segment_id"] = _stable("SEG", segment["segment_id"], segment.get("name"))
            used.add(segment["segment_id"]); segment["member_event_ids"] = []
        assignments_by_id = {}
        coverage_round = self._coverage_round(workdir, "assignment")
        assignment_chunks = []
        for batch_number, chunk in enumerate(_chunks(events, ASSIGNMENT_EVENTS_PER_CALL), 1):
            # Keep deterministic base IDs stable across pipeline retries. Input
            # hashes already invalidate changed payloads; bumping this ID after
            # a coverage failure would reissue every completed base assignment.
            batch_id = "assignment-r00-%05d" % batch_number
            payload = {"events": chunk, "segments": segments,
                       "required_event_ids": sorted(event["event_id"] for event in chunk)}
            assignment_chunks.append((batch_id, chunk, payload))
        base_results = self._call_many(run, revision, workdir, "assign_segments",
                                       [(batch_id, payload) for batch_id, _, payload in assignment_chunks])
        for (base_batch_id, chunk, _), assignment in zip(assignment_chunks, base_results):
            pending = list(chunk)
            for attempt in range(4):
                if not pending:
                    break
                requested_ids = {event["event_id"] for event in pending}
                if attempt:
                    if attempt <= 2:
                        batch_id = base_batch_id + "-coverage-%02d" % attempt
                    else:
                        batch_id = base_batch_id + "-coverage-r%02d-%02d" % (coverage_round, attempt)
                    repair_schema = json.loads(json.dumps(SCHEMAS["assign_segments"]))
                    repair_schema["properties"]["assignments"]["items"]["properties"]["segment_id"]["enum"] = sorted(used)
                    assignment = self._call(run, revision, workdir, "assign_segments", batch_id,
                                            {"events": pending, "segments": segments,
                                             "required_event_ids": sorted(requested_ids)},
                                            schema_override=repair_schema)
                for row in assignment.get("assignments") or []:
                    event_id = row.get("event_id")
                    if event_id in requested_ids and row.get("segment_id") in used:
                        assignments_by_id.setdefault(event_id, row)
                pending = [event for event in pending if event["event_id"] not in assignments_by_id]
            if pending:
                self._advance_coverage_round(workdir, "assignment")
                raise ValueError("细分归属仍缺少 %s 个事件" % len(pending))
        by_segment = {s["segment_id"]: s for s in segments}
        for row in assignments_by_id.values():
            if row.get("segment_id") in by_segment:
                by_segment[row["segment_id"]]["member_event_ids"].append(row.get("event_id"))
        event_by_id = {e["event_id"]: e for e in events}
        for segment in segments:
            reviews = {event_by_id[eid]["review_id"] for eid in segment["member_event_ids"] if eid in event_by_id}
            segment["status"] = "formal" if len(reviews) >= minimum else "exploratory"
            segment["evidence_event_ids"] = segment["member_event_ids"][:20]
        proposed_segment_count = len(segments)
        inactive_segments = [segment for segment in segments if not segment["member_event_ids"]]
        segments = [segment for segment in segments if segment["member_event_ids"]]
        if not segments:
            raise ValueError("细分归属后没有包含样本事件的有效细分")
        metadata = {"model": self.settings.extractor_model, "model_version": self.settings.extractor_model,
                    "prompt_version": self.prompts.load("propose_segments")[0]["version"], "generated_at": utcnow(),
                    "proposal_mode": "profile_batches" if len(proposal_batches) > 1 else "single_profile_batch",
                    "proposal_batch_count": len(proposal_batches),
                    "proposal_profile_count": len(proposal_input.get("profiles") or []),
                    "proposed_segment_count": proposed_segment_count,
                    "inactive_candidate_count": len(inactive_segments),
                    "proposal_event_count": len(events),
                    "proposal_input_bytes": _json_size_bytes(proposal_input),
                    "proposal_max_batch_bytes": max((batch["input_bytes"] for batch in proposal_batches), default=0)}
        _write(cache / "segment_model.json", {"schema": "amazon-voc-segment-model/v3", "metadata": metadata,
                                               "segments": segments, "journey": {}})

    def _journey(self, run, revision, workdir):
        cache = Path(workdir) / "cache"
        events = _read(cache / "events.json")["events"]
        doc = _read(cache / "segment_model.json")
        segment_by_event = {event_id: segment["segment_id"]
                            for segment in doc["segments"]
                            for event_id in segment.get("member_event_ids") or []}
        stage_proposal = build_journey_stage_proposal_input(events, doc["segments"])
        stage_payload = {**stage_proposal, "event_assignments": [], "fixed_stages": []}
        stages_result = self._call(
            run, revision, workdir, "build_journey", "journey-stages",
            stage_payload)
        stages = stages_result.get("stages") or []
        if not stages:
            raise ValueError("旅程阶段目录为空")
        assignments = []
        coverage_round = self._coverage_round(workdir, "journey")
        journey_chunks = []
        for batch_number, chunk in enumerate(_chunks(events, JOURNEY_EVENTS_PER_CALL), 1):
            chunk_ids = {event.get("event_id") for event in chunk}
            chunk_segment_ids = {segment_by_event[event_id] for event_id in chunk_ids
                                 if event_id in segment_by_event}
            chunk_segments = [{key: segment.get(key, "") for key in
                               ("segment_id", "name", "segment_key", "persona_summary", "jtbd_statement")}
                              for segment in doc["segments"] if segment["segment_id"] in chunk_segment_ids]
            chunk_memberships = [{"event_id": event_id, "segment_id": segment_id}
                                 for event_id, segment_id in segment_by_event.items()
                                 if event_id in chunk_ids]
            batch_id = "journey-map-r%02d-%05d" % (coverage_round, batch_number)
            payload = {"events": chunk, "segments": chunk_segments,
                       "event_assignments": chunk_memberships, "fixed_stages": stages,
                       "required_event_ids": sorted(chunk_ids)}
            journey_chunks.append((batch_id, chunk, chunk_ids, chunk_segment_ids, chunk_memberships,
                                   chunk_segments, payload))
        base_results = self._call_many(run, revision, workdir, "build_journey",
                                       [(batch_id, payload) for batch_id, _, _, _, _, _, payload in journey_chunks])
        for (base_batch_id, chunk, chunk_ids, chunk_segment_ids, chunk_memberships, chunk_segments, _), result in zip(journey_chunks, base_results):
            assigned_ids = set()
            pending_ids = set(chunk_ids)
            for row in result.get("assignments") or []:
                if (row.get("event_id") in pending_ids and row.get("event_id") not in assigned_ids and
                        row.get("stage_id") in {s["stage_id"] for s in stages}):
                    assignments.append(row)
                    assigned_ids.add(row["event_id"])
            pending_ids -= assigned_ids
            for attempt in range(3):
                if not pending_ids:
                    break
                pending_events = [event for event in chunk if event.get("event_id") in pending_ids]
                pending_memberships = [row for row in chunk_memberships if row["event_id"] in pending_ids]
                batch_id = base_batch_id + "-coverage-%02d" % (attempt + 1)
                result = self._call(
                    run, revision, workdir, "build_journey", batch_id,
                    {"events": pending_events, "segments": chunk_segments,
                     "event_assignments": pending_memberships, "fixed_stages": stages,
                     "required_event_ids": sorted(pending_ids)})
                for row in result.get("assignments") or []:
                    if row.get("event_id") in pending_ids and row.get("stage_id") in {s["stage_id"] for s in stages}:
                        if row["event_id"] not in assigned_ids:
                            assignments.append(row)
                            assigned_ids.add(row["event_id"])
                pending_ids = chunk_ids - assigned_ids
            if assigned_ids != chunk_ids:
                self._advance_coverage_round(workdir, "journey")
                raise ValueError("旅程阶段归属仍缺少 %s 个事件" % len(chunk_ids - assigned_ids))
        doc["journey"] = {"stages": stages,
                          "stage_generation": {
                              "method": stage_proposal["sampling_method"],
                              "event_count": stage_proposal["event_count"],
                              "sampled_event_count": stage_proposal["sampling"]["sampled_event_count"],
                              "active_segment_count": stage_proposal["sampling"]["active_segment_count"],
                              "max_samples_per_segment": stage_proposal["sampling"]["max_samples_per_segment"],
                              "input_bytes": _json_size_bytes(stage_payload),
                          },
                          "event_assignments": [{"event_id": row.get("event_id"), "stage_id": row.get("stage_id")}
                                                for row in assignments]}
        _write(cache / "segment_model.json", doc)

    def _compile(self, run, revision, workdir):
        report = evaluate(Path(workdir), include_outputs=False)
        if not report["precompile_green"]:
            raise ValueError("质量门禁未通过，已停止报告生成")
        self._run_script("compile_analysis.py", "--workdir", workdir)
        self._run_script("build_html.py", "--workdir", workdir)
        self._run_script("build_excel.py", "--workdir", workdir)
        report = evaluate(Path(workdir), include_outputs=True)
        if not report["all_green"]:
            raise ValueError("输出一致性门禁未通过")
        analysis_path = Path(workdir) / "cache" / "analysis.json"
        analysis = _read(analysis_path); analysis["quality"] = report; _write(analysis_path, analysis)
        self._run_script("build_html.py", "--workdir", workdir)
        self._run_script("build_excel.py", "--workdir", workdir)
        self._run_script("validate.py", "--workdir", workdir)
        for kind in ("html", "excel", "analysis", "quality", "calls"):
            path = self.storage.artifact(run["project_id"], run["id"], revision, kind)
            if path:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                self.db.execute("INSERT INTO artifacts(run_id,revision,kind,path,sha256,created_at) VALUES(?,?,?,?,?,?) "
                                "ON CONFLICT(run_id,revision,kind) DO UPDATE SET path=excluded.path,sha256=excluded.sha256,created_at=excluded.created_at",
                                (run["id"], revision, kind, str(path), digest, utcnow()))

    def process(self, run_id):
        run = self.db.get_run(run_id)
        if not run or run["status"] in TERMINAL:
            return
        revision = int(run["current_revision"])
        workdir = self.storage.revision_dir(run["project_id"], run_id, revision)
        try:
            cache = workdir / "cache"
            for legacy_name in ("raw_alexa.json", "cleaned_alexa.json", "external_enrichment.json",
                                "manual_external_sources.json", "recommendations.json"):
                legacy_path = cache / legacy_name
                if legacy_path.exists():
                    legacy_path.unlink()
            if not (workdir / "cache" / "cleaned_reviews.json").exists():
                self._initialize(run, workdir); self._status(run_id, "cleaned")
            if not (workdir / "cache" / "events.json").exists():
                self._status(run_id, "extracting"); self._extract(run, revision, workdir)
            self._status(run_id, "extracting"); self._reconcile_extraction_coverage(run, revision, workdir)
            if not self._review_cache_complete(workdir):
                self._status(run_id, "reviewing"); self._review(run, revision, workdir)
            self._withhold_unbounded_inferences(workdir)
            self._status(run_id, "normalizing"); self._normalize(run, revision, workdir)
            self._status(run_id, "segmenting"); self._segment(run, revision, workdir)
            self._status(run_id, "journey"); self._journey(run, revision, workdir)
            self._export_calls(run, revision, workdir)
            self._status(run_id, "compiling"); self._compile(run, revision, workdir)
            self.db.update_run(run_id, status="completed", completed_at=utcnow(), error="")
        except InterruptedError:
            return
        except Exception as exc:
            self._export_calls(run, revision, workdir)
            self.db.update_run(run_id, status="failed", error=str(exc)[:4000], completed_at=utcnow())
            raise
