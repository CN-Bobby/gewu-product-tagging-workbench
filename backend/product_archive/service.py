"""Persistence and read-only Dataset binding for market dossiers."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, func

from ..db import STORAGE, Session, Dataset, Product, Asset, Snapshot, Workspace, obj, require, uid
from .. import importer
from .analytics import compute_metrics, number
from .fields import FIELD_ROLES, OVERVIEW_FIELDS, default_mapping

ROOT = STORAGE / "product-archive"
ARCHIVES = ROOT / "archives"
MARKET_RE = re.compile(r"^[A-Z]{2,3}$")
MONTH_RE = re.compile(r"^\d{4}[-/]?(0[1-9]|1[0-2])$")
ASIN_VALUE_RE = re.compile(r"^B[0-9A-Z]{9}$", re.IGNORECASE)
ASIN_URL_RE = re.compile(r"(?:/dp/|/gp/product/|[?&]asin=)(B[0-9A-Z]{9})(?:[/?&#]|$)", re.IGNORECASE)


def _path(archive_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", archive_id or ""):
        raise ValueError("选品档案编号无效")
    return ARCHIVES / f"{archive_id}.json"


def _save(record: dict) -> dict:
    ARCHIVES.mkdir(parents=True, exist_ok=True)
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    target = _path(record["id"])
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)
    workspace_id = record.get("workspace_id")
    if workspace_id:
        from .. import project_files
        project_files.register_file(workspace_id, "product_archive", target,
            source_type="product_archive", source_id=record["id"], name=f"{record['id']}.json",
            metadata={"dataset_id": record.get("dataset_id"), "mapping_confirmed": bool(record.get("mapping_confirmed"))})
    return record


def _read(archive_id: str) -> dict:
    target = _path(archive_id)
    if not target.exists():
        raise ValueError("选品档案不存在")
    return json.loads(target.read_text(encoding="utf-8"))


def _source(dataset_id: str):
    with Session() as session:
        dataset = require(session, Dataset, dataset_id)
        products = session.scalars(select(Product).where(Product.dataset_id == dataset_id).order_by(Product.row)).all()
        return obj(dataset), [obj(product) for product in products]


def _currency(marketplace: str) -> str | None:
    return {"US": "USD", "GB": "GBP", "UK": "GBP", "CA": "CAD", "DE": "EUR",
            "FR": "EUR", "ES": "EUR", "IT": "EUR", "JP": "JPY", "IN": "INR",
            "MX": "MXN", "BR": "BRL", "AU": "AUD", "AE": "AED", "SA": "SAR"}.get(marketplace)


def _add_assets(metrics: dict, dataset_id: str, mapping: dict, rows: list[dict]) -> None:
    """Add only imported local images; never fetch external image URLs while rendering an archive."""
    with Session() as session:
        records = session.execute(select(Product, Asset).join(Asset, Asset.product_id == Product.id)
            .where(Product.dataset_id == dataset_id, Asset.path.is_not(None))
            .order_by(Product.row, Asset.image_type, Asset.created_at)).all()
    by_product: dict[str, str] = {}
    for product, asset in records:
        if not asset.path or not Path(asset.path).is_file():
            continue
        current = by_product.get(product.id)
        if current is None or asset.image_type == "main":
            by_product[product.id] = f"/api/assets/{asset.id}"
    by_asin: dict[str, dict] = {}
    by_brand: dict[str, list[dict]] = {}
    asin_header = mapping.get("asin")
    brand_header = mapping.get("brand")
    sales_header = mapping.get("monthly_sales")
    if not asin_header:
        return
    for row in rows:
        raw = row.get("raw") or {}
        asin = str(raw.get(asin_header) or "").strip()
        if not asin:
            continue
        product_id = row.get("id")
        image_url = by_product.get(product_id)
        entry = {"asin": asin, "units": number(raw.get(sales_header)) if sales_header else None,
                 "image_url": image_url}
        entry["units"] = entry["units"] or 0.0
        prior = by_asin.get(asin.casefold())
        if prior is None or entry["units"] > prior["units"]:
            by_asin[asin.casefold()] = entry
        brand = str(raw.get(brand_header) or "").strip() if brand_header else ""
        if brand:
            items = by_brand.setdefault(brand.casefold(), [])
            old = next((item for item in items if item["asin"].casefold() == asin.casefold()), None)
            if old is None:
                items.append(entry)
            elif entry["units"] > old["units"]:
                items[items.index(old)] = entry
    competition = metrics.get("competition") or {}
    for entry in (competition.get("asin") or {}).get("top_by_units", []):
        found = by_asin.get(str(entry.get("name") or "").casefold())
        if found:
            entry["image_url"] = found.get("image_url")
    for brand_entry in (competition.get("brand") or {}).get("top_by_units", []):
        brand_rows = by_brand.get(str(brand_entry.get("name") or "").casefold(), [])
        brand_entry["images"] = sorted(brand_rows, key=lambda item: (-item["units"], item["asin"].casefold()))[:20]


def _price_points(metrics: dict) -> None:
    distribution = (metrics.get("competition") or {}).get("price") or {}
    total_links = int(metrics.get("analysis_rows") or 0)
    total_units = float(metrics.get("sample_units") or 0)
    distribution["price_points"] = [{
        "price": item["price"], "currency": distribution.get("currency"),
        "units": item["units"], "unit_share": round(item["units"] / total_units, 6) if total_units else None,
        "link_count": item["link_count"],
        "link_share": round(item["link_count"] / total_links, 6) if total_links else None,
    } for item in distribution.get("exact_prices", [])]


def _slice_source(dataset_id: str, rows: list[dict], filters: dict | None):
    """Build slicers from the latest immutable Data X snapshot and filter by saved tag values."""
    filters = filters if isinstance(filters, dict) else {}
    with Session() as session:
        snapshot = session.scalar(select(Snapshot).where(Snapshot.dataset_id == dataset_id)
                                  .order_by(Snapshot.version.desc()).limit(1))
        snapshot_data = snapshot.data if snapshot else None
    if not isinstance(snapshot_data, dict):
        if filters:
            raise ValueError("当前 Dataset 尚无已保存的 Data X 版本，不能按标签筛选")
        return rows, [], {}, None

    columns = [item for item in snapshot_data.get("columns", [])
               if not (item.get("definition") or {}).get("archived")]
    snapshot_rows = {item.get("id"): item for item in snapshot_data.get("rows", []) if item.get("id")}
    slicers = []
    valid_filters = {}
    for column in columns:
        column_id = column.get("id")
        definition = column.get("definition") or {}
        if not column_id:
            continue
        counts: dict[str, dict[str, Any]] = {}
        for row in snapshot_rows.values():
            cell = (row.get("cells") or {}).get(column_id) or {}
            if cell.get("status") not in ("ready", "review"):
                continue
            value = cell.get("manual") if cell.get("manual") is not None else cell.get("value")
            values = value if isinstance(value, list) else [value]
            for item in values:
                if item is None or isinstance(item, str) and not item.strip():
                    continue
                key = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                entry = counts.setdefault(key, {"value": key, "label": str(item), "link_count": 0})
                entry["link_count"] += 1
        if not counts:
            continue
        values = sorted(counts.values(), key=lambda item: (-item["link_count"], item["label"].casefold()))
        slicers.append({"id": column_id, "key": column_id, "label": definition.get("name") or definition.get("key") or column_id,
                        "values": values})

    slicer_by_id = {item["id"]: item for item in slicers}
    for column_id, selected in filters.items():
        if column_id not in slicer_by_id:
            raise ValueError("筛选标签不属于当前 Data X 版本")
        if selected not in {item["value"] for item in slicer_by_id[column_id]["values"]}:
            raise ValueError("所选标签值不在当前 Data X 版本中")
        valid_filters[column_id] = selected

    if not valid_filters:
        return rows, slicers, {}, snapshot

    selected_product_ids = set()
    for product_id, row in snapshot_rows.items():
        matches = True
        for column_id, selected in valid_filters.items():
            cell = (row.get("cells") or {}).get(column_id) or {}
            if cell.get("status") not in ("ready", "review"):
                matches = False
                break
            value = cell.get("manual") if cell.get("manual") is not None else cell.get("value")
            selected_value = json.loads(selected)
            choices = value if isinstance(value, list) else [value]
            if selected_value not in choices:
                matches = False
                break
        if matches:
            selected_product_ids.add(product_id)
    filtered = [row for row in rows if row.get("id") in selected_product_ids]
    return filtered, slicers, valid_filters, snapshot


def _source_digest(dataset: dict, rows: list[dict]) -> str:
    payload = {"dataset_id": dataset["id"], "dataset_sha256": dataset.get("sha256"),
               "rows": [{"row": row.get("row"), "raw": row.get("raw") or {}} for row in rows]}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _mapping_digest(mapping: dict) -> str:
    encoded = json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _summary(dataset: dict) -> dict:
    with Session() as session:
        count = session.scalar(select(func.count()).select_from(Product).where(Product.dataset_id == dataset["id"])) or 0
        workspace = session.get(Workspace, dataset.get("workspace_id"))
        return {"id": dataset["id"], "name": dataset.get("name"), "version": dataset.get("version"),
                "source_filename": dataset.get("source_filename"), "sheet": dataset.get("sheet"),
                "sha256": dataset.get("sha256"), "count": count,
                "workspace_id": dataset.get("workspace_id"), "workspace_name": workspace.name if workspace else None}


def list_datasets() -> list[dict]:
    from ..service import list_datasets as tagging_datasets
    return tagging_datasets()


def create(dataset_id: str, marketplace: str, category: str = "", data_month: str = "") -> dict:
    marketplace = str(marketplace or "").strip().upper()
    category = str(category or "").strip()
    data_month = str(data_month or "").strip().replace("/", "-")
    if not MARKET_RE.fullmatch(marketplace):
        raise ValueError("站点请使用两到三位大写国家代码")
    if data_month and not MONTH_RE.fullmatch(data_month.replace("-", "")):
        raise ValueError("数据月份格式应为 YYYY-MM")
    dataset, rows = _source(dataset_id)
    record = {
        "id": uid(), "created_at": datetime.now(timezone.utc).isoformat(),
        "title": category or dataset.get("name") or "新建选品档案", "marketplace": marketplace,
        "category": category, "data_month": data_month, "dataset_id": dataset_id,
        "workspace_id": dataset.get("workspace_id"),
        "overview": {"product_name": "", "representative_keywords": "", "product_function": "",
                     "target_audience": "", "cultural_context": "", "seasonality": ""},
        "mapping": default_mapping(dataset.get("headers"), dataset.get("mapping")),
        "mapping_confirmed": False,
        "source_snapshot": {"dataset_sha256": dataset.get("sha256"), "rows_sha256": _source_digest(dataset, rows),
                            "row_count": len(rows), "captured_at": None},
        "mapping_revision": 0,
    }
    _save(record)
    return get(record["id"])


def ensure_dataset_archive(dataset_id: str, marketplace: str) -> dict:
    """Return the project's existing dossier for this Dataset/site, or create a draft one.

    The default field mapping remains unconfirmed. A downstream task must not start until
    the user has reviewed that mapping on the archive's sample page.
    """
    marketplace = str(marketplace or "").strip().upper()
    if not MARKET_RE.fullmatch(marketplace):
        raise ValueError("站点请使用两到三位大写国家代码")
    dataset, _ = _source(dataset_id)
    if not dataset.get("workspace_id"):
        raise ValueError("当前数据集没有所属项目，请先在商品打标页关联项目")
    matches = []
    if ARCHIVES.exists():
        for target in ARCHIVES.glob("*.json"):
            try:
                record = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (record.get("dataset_id") == dataset_id
                    and str(record.get("marketplace") or "").upper() == marketplace):
                matches.append(record)
    if matches:
        record = max(matches, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""))
        return get(record["id"])
    return create(dataset_id, marketplace)


def import_workbook(data: bytes, filename: str, marketplace: str, category: str = "",
                    data_month: str = "", workspace_id: str = "") -> dict:
    if not filename.lower().endswith(".xlsx"):
        raise ValueError("请上传 .xlsx 文件")
    dataset = importer.import_file(data, filename, workspace_id=workspace_id or None)
    # This dedicated path deliberately does not call text_facts.prepare or launch a model task.
    return create(dataset["id"], marketplace, category, data_month)


def bind_dataset(archive_id: str, dataset_id: str) -> dict:
    record = _read(archive_id)
    dataset, rows = _source(dataset_id)
    record["dataset_id"] = dataset_id
    record["workspace_id"] = dataset.get("workspace_id")
    record["mapping"] = default_mapping(dataset.get("headers"), dataset.get("mapping"))
    record["mapping_confirmed"] = False
    record["source_snapshot"] = {"dataset_sha256": dataset.get("sha256"), "rows_sha256": _source_digest(dataset, rows),
                                  "row_count": len(rows), "captured_at": None}
    record["mapping_revision"] = int(record.get("mapping_revision") or 0) + 1
    _save(record)
    return get(archive_id)


def import_into_archive(archive_id: str, data: bytes, filename: str, workspace_id: str = "") -> dict:
    if not filename.lower().endswith(".xlsx"):
        raise ValueError("请上传 .xlsx 文件")
    dataset = importer.import_file(data, filename, workspace_id=workspace_id or None)
    return bind_dataset(archive_id, dataset["id"])


def _check_snapshot(record: dict, dataset: dict, rows: list[dict]) -> None:
    snapshot = record.get("source_snapshot") or {}
    if (snapshot.get("dataset_sha256") != dataset.get("sha256")
            or snapshot.get("rows_sha256") != _source_digest(dataset, rows)
            or snapshot.get("mapping_sha256") != _mapping_digest(record.get("mapping") or {})):
        raise ValueError("样本来源已变化；请重新确认字段映射以冻结新快照")


def get(archive_id: str, filters: dict | None = None) -> dict:
    record = _read(archive_id)
    record.setdefault("overview_suggestions", {})
    dataset, source_rows = _source(record["dataset_id"])
    if record.get("mapping_confirmed"):
        _check_snapshot(record, dataset, source_rows)
    rows, tag_slicers, current_filter, data_x = _slice_source(dataset["id"], source_rows, filters)
    result = {**record, "dataset": _summary(dataset)}
    result["tag_slicers"] = tag_slicers
    result["current_filter"] = current_filter
    result["data_x_version"] = {"id": data_x.id, "version": data_x.version,
                                "created_at": data_x.created_at} if data_x else None
    if record.get("mapping_confirmed"):
        mapping = record.get("mapping") or {}
        metrics = compute_metrics(rows, mapping, record.get("data_month"),
                                  (record.get("source_snapshot") or {}).get("captured_at"),
                                  _currency(record.get("marketplace") or ""))
        _price_points(metrics)
        _add_assets(metrics, dataset["id"], mapping, rows)
        keyword_summary = (((record.get("integrations") or {}).get("keyword_research") or {})
                           .get("summary") or {})
        if keyword_summary:
            metrics["market_size"].update({
                "keyword_search_volume": keyword_summary.get("searches_median"),
                "keyword_search_volume_status": "available" if keyword_summary.get("searches_median") is not None
                else "no_numeric_signal",
                "keyword_search_volume_basis": "median_of_unique_representative_terms_non_additive",
            })
        if filters:
            metrics["base_analysis_rows"] = len(source_rows)
            metrics["filtered_analysis_rows"] = len(rows)
        result["metrics"] = metrics
    else:
        result["metrics"] = None
    return result


def list_archives() -> list[dict]:
    if not ARCHIVES.exists():
        return []
    result = []
    for target in sorted(ARCHIVES.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            record = json.loads(target.read_text(encoding="utf-8"))
            dataset, _ = _source(record["dataset_id"])
            result.append({"id": record["id"], "title": record.get("title"), "marketplace": record.get("marketplace"),
                           "category": record.get("category"), "data_month": record.get("data_month"),
                           "dataset": _summary(dataset), "mapping_confirmed": record.get("mapping_confirmed", False),
                           "updated_at": record.get("updated_at")})
        except (ValueError, KeyError, OSError, json.JSONDecodeError):
            continue
    return result


def update_overview(archive_id: str, body: dict) -> dict:
    record = _read(archive_id)
    expected = body.get("expected_updated_at")
    if expected is not None and str(expected) != str(record.get("updated_at") or ""):
        raise ValueError("档案内容已被其他操作更新，请刷新后重试")
    overview = record.setdefault("overview", {})
    sources = record.setdefault("overview_sources", {})
    suggestions = record.setdefault("overview_suggestions", {})
    accepted_ids = body.get("accepted_suggestion_ids") or []
    if not isinstance(accepted_ids, list) or any(not isinstance(item, str) for item in accepted_ids):
        raise ValueError("accepted_suggestion_ids 必须是字符串数组")
    ignored_ids = body.get("ignored_suggestion_ids") or []
    if not isinstance(ignored_ids, list) or any(not isinstance(item, str) for item in ignored_ids):
        raise ValueError("ignored_suggestion_ids 必须是字符串数组")
    if set(accepted_ids) & set(ignored_ids):
        raise ValueError("建议不能同时采纳和忽略")
    found = {}
    for field, items in suggestions.items():
        if field not in OVERVIEW_FIELDS or not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("id") in set(accepted_ids) | set(ignored_ids):
                found[item["id"]] = (field, item)
    if (set(accepted_ids) | set(ignored_ids)) - set(found):
        raise ValueError("待确认建议已不存在，请刷新后重试")
    for ident in accepted_ids:
        field, item = found[ident]
        if field not in body:
            body[field] = item.get("text", "")
    for key in OVERVIEW_FIELDS:
        if key not in body:
            continue
        value = body[key]
        if not isinstance(value, str) or len(value) > 4000:
            raise ValueError(f"{OVERVIEW_FIELDS[key]}最多 4000 个字符")
        normalized = value.strip()
        if normalized != str(overview.get(key) or "").strip():
            accepted = next((item for item in accepted_ids if found[item][0] == key), None)
            item = found.get(accepted, (None, {}))[1]
            sources[key] = {"source_type": "manual" if not accepted or normalized != str(item.get("text") or "").strip()
                            else item.get("source_type", "manual"),
                            "source_ids": [] if not accepted or normalized != str(item.get("text") or "").strip()
                            else item.get("source_ids", []),
                            "updated_at": datetime.now(timezone.utc).isoformat()}
        overview[key] = normalized
    retired_ids = set(accepted_ids) | set(ignored_ids)
    for field, items in list(suggestions.items()):
        kept = [item for item in items if not isinstance(item, dict) or item.get("id") not in retired_ids]
        if kept:
            suggestions[field] = kept
        else:
            suggestions.pop(field, None)
    for key, label in (("marketplace", "站点"), ("category", "品类"), ("data_month", "数据月份")):
        if key not in body:
            continue
        value = str(body[key] or "").strip()
        if key == "marketplace":
            value = value.upper()
            if not MARKET_RE.fullmatch(value): raise ValueError("站点请使用两到三位大写国家代码")
        if key == "data_month" and value and not MONTH_RE.fullmatch(value.replace("-", "")):
            raise ValueError("数据月份格式应为 YYYY-MM")
        if key == "category" and len(value) > 300: raise ValueError("品类最多 300 个字符")
        record[key] = value
    _save(record)
    return get(archive_id)


def confirm_mapping(archive_id: str, mapping: dict) -> dict:
    record = _read(archive_id)
    dataset, rows = _source(record["dataset_id"])
    headers = set(dataset.get("headers") or [])
    if not isinstance(mapping, dict):
        raise ValueError("字段映射必须是对象")
    unknown_roles = set(mapping).difference(FIELD_ROLES)
    if unknown_roles:
        raise ValueError("包含不支持的映射字段")
    normalized = {role: mapping.get(role) or None for role in FIELD_ROLES}
    invalid = [header for header in normalized.values() if header is not None and header not in headers]
    if invalid:
        raise ValueError("映射列必须来自该样本的原始表头")
    record["mapping"] = normalized
    record["mapping_confirmed"] = True
    record["mapping_revision"] = int(record.get("mapping_revision") or 0) + 1
    record["source_snapshot"] = {"dataset_sha256": dataset.get("sha256"), "rows_sha256": _source_digest(dataset, rows),
                                  "row_count": len(rows), "captured_at": datetime.now(timezone.utc).isoformat(),
                                  "mapping_revision": record["mapping_revision"],
                                  "mapping_sha256": _mapping_digest(normalized)}
    _save(record)
    return get(archive_id)


def preview(archive_id: str, query: str = "", offset: int = 0, limit: int = 50) -> dict:
    record = _read(archive_id)
    dataset, rows = _source(record["dataset_id"])
    if record.get("mapping_confirmed"):
        _check_snapshot(record, dataset, rows)
    mapping = record.get("mapping") or {}
    asin_header = mapping.get("asin")
    query = str(query or "").strip().casefold()
    filtered = [row for row in rows if not query or query in str((row.get("raw") or {}).get(asin_header) or row.get("anchor") or "").casefold()]
    anchors: dict[str, int] = {}
    for row in rows:
        raw = row.get("raw") or {}
        key = str((raw.get(asin_header) if asin_header else row.get("anchor")) or "").strip().casefold()
        if key: anchors[key] = anchors.get(key, 0) + 1
    duplicates = sum(count - 1 for count in anchors.values() if count > 1)
    missing = {}
    for role, header in mapping.items():
        if not header: continue
        missing[role] = sum(1 for row in rows if _blank((row.get("raw") or {}).get(header)))
    offset = max(0, int(offset))
    limit = min(100, max(1, int(limit)))
    return {"archive_id": archive_id, "dataset": _summary(dataset), "headers": dataset.get("headers") or [],
            "mapping": mapping, "mapping_confirmed": bool(record.get("mapping_confirmed")),
            "total": len(filtered), "offset": offset, "limit": limit,
            "duplicate_rows": duplicates, "duplicate_identifiers": sum(1 for count in anchors.values() if count > 1),
            "missing_by_role": missing,
            "rows": [{"id": row.get("id"), "row": row.get("row"), "anchor": row.get("anchor"), "raw": row.get("raw") or {}}
                     for row in filtered[offset:offset + limit]]}


def dispatch_targets(archive_id: str, product_ids: list[str], *, child_only: bool = False) -> dict:
    """Resolve explicitly selected source rows to their own ASINs, preferring detail URLs."""
    if not isinstance(product_ids, list) or not product_ids:
        raise ValueError("请至少选择一条商品记录")
    if len(product_ids) > 100:
        raise ValueError("一次最多选择 100 条商品记录")
    requested = list(dict.fromkeys(str(item) for item in product_ids if item))
    record = _read(archive_id)
    dataset, _ = _source(record["dataset_id"])
    if not record.get("mapping_confirmed"):
        raise ValueError("请先确认样本映射，再选择商品创建下游任务")
    mapping = record.get("mapping") or {}
    asin_header, url_header = mapping.get("asin"), mapping.get("url")
    if not asin_header:
        raise ValueError("档案尚未映射 ASIN 列")
    if child_only and not url_header:
        raise ValueError("档案尚未映射商品详情链接列")
    with Session() as session:
        products = session.scalars(select(Product).where(Product.dataset_id == dataset["id"],
            Product.id.in_(requested))).all()
    by_id = {item.id: item for item in products}
    if len(by_id) != len(requested):
        raise ValueError("选择中包含不属于当前档案 Dataset 的商品记录")
    targets = []
    seen = set()
    unresolved = 0
    for product_id in requested:
        raw = by_id[product_id].raw or {}
        url = str(raw.get(url_header) or "") if url_header else ""
        match = ASIN_URL_RE.search(url)
        candidate = match.group(1).upper() if match else ""
        if not candidate and not child_only:
            raw_asin = str(raw.get(asin_header) or "").strip().upper()
            candidate = raw_asin if ASIN_VALUE_RE.fullmatch(raw_asin) else ""
        if not candidate:
            unresolved += 1
            continue
        if candidate not in seen:
            targets.append(candidate)
            seen.add(candidate)
    if unresolved:
        raise ValueError(f"有 {unresolved} 条选择记录无法解析到所需 ASIN；请补充商品详情页链接后重试")
    return {"archive_id": archive_id, "dataset_id": dataset["id"],
            "workspace_id": dataset.get("workspace_id"), "asins": targets,
            "selected_rows": len(requested), "unique_asins": len(targets)}


def _blank(value) -> bool:
    return value is None or isinstance(value, str) and not value.strip()
