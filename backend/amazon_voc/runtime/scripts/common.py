# -*- coding: utf-8 -*-
"""Amazon VOC V4 shared deterministic helpers.

This module intentionally contains no category, marketplace, persona, journey,
or Kano keyword constants. Those structures are generated per task from the
evidence and validated against the V4 contracts.
"""
import hashlib
import json
import os
import re


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, value):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def cache_path(workdir, name):
    return os.path.join(workdir, "cache", name)


def output_path(workdir, name):
    return os.path.join(workdir, "output", name)


def ensure_dirs(workdir):
    os.makedirs(cache_path(workdir, ""), exist_ok=True)
    os.makedirs(output_path(workdir, ""), exist_ok=True)


def norm(value):
    """Normalize a header or alias for tolerant matching."""
    if value is None:
        return ""
    return "".join(str(value).lower().split())


def load_aliases(path):
    return read_json(path) if path and os.path.exists(path) else {}


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def unique(values):
    seen = set()
    result = []
    for value in values:
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        if value in (None, "", [], {}) or marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result


def stable_id(prefix, *parts):
    canonical = "\x1f".join(clean_text(part).lower() for part in parts)
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:8].upper()
    readable = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", clean_text(parts[0] if parts else ""))
    readable = readable.strip("-")[:18] or "item"
    return "%s-%s-%s" % (prefix, readable, digest)


def independent_review_ids(events):
    return {item.get("review_id") for item in events if item.get("review_id")}


def independent_asins(events):
    return {item.get("asin") for item in events if item.get("asin")}


def review_coverage(events, denominator):
    return round(len(independent_review_ids(events)) / denominator, 6) if denominator else 0.0


def as_list(value):
    if value in (None, ""):
        return []
    return value if isinstance(value, list) else [value]


def nested(item, *keys):
    value = item
    for key in keys:
        if not isinstance(value, dict):
            return ""
        value = value.get(key)
    return value


def count_values(events, path, review_level=True):
    """Count values, deduplicating repeated events from the same review by default."""
    counts = {}
    seen = set()
    for event in events:
        values = as_list(nested(event, *path))
        for value in values:
            text = clean_text(value)
            if not text:
                continue
            marker = (event.get("review_id"), text) if review_level else (event.get("event_id"), text)
            if marker in seen:
                continue
            seen.add(marker)
            counts[text] = counts.get(text, 0) + 1
    return [{"label": key, "review_count": value} for key, value in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))]
