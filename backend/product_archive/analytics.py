"""Deterministic, sample-scoped calculations for Product Archive."""

from __future__ import annotations

import calendar
import math
import re
from datetime import date, datetime
from typing import Any


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if math.isfinite(result) else None
    text = str(value).strip()
    if not text or text in {"-", "—", "N/A", "n/a", "未知", "無", "无"}:
        return None
    text = text.replace(",", "").replace("$", "").replace("€", "").replace("£", "")
    match = re.search(r"[-+]?(?:\d+\.?\d*|\.\d+)", text)
    if not match:
        return None
    try:
        result = float(match.group(0))
        return result if math.isfinite(result) else None
    except ValueError:
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _ratio(part: float, total: float) -> float | None:
    return round(part / total, 6) if total > 0 else None


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    if not text:
        return None
    text = text[:10].replace("/", "-").replace(".", "-")
    for pattern in ("%Y-%m-%d", "%Y-%m", "%Y%m%d", "%Y%m"):
        try:
            parsed = datetime.strptime(text, pattern).date()
            if pattern in ("%Y-%m", "%Y%m"):
                return parsed.replace(day=1)
            return parsed
        except ValueError:
            pass
    return None


def _rank_shares(rows: list[dict], key: str, units_total: float, revenue_total: float):
    totals: dict[str, dict[str, float]] = {}
    for row in rows:
        label = _text(row.get(key))
        if not label:
            continue
        entry = totals.setdefault(label, {"units": 0.0, "revenue": 0.0})
        entry["units"] += row["units"] or 0.0
        entry["revenue"] += row["revenue"] or 0.0
    ranked_units = sorted(totals.items(), key=lambda item: (-item[1]["units"], item[0].casefold()))
    ranked_revenue = sorted(totals.items(), key=lambda item: (-item[1]["revenue"], item[0].casefold()))

    def concentration(ranked, amount_key, total):
        values = []
        for size in (3, 5, 10):
            amount = sum(item[1][amount_key] for item in ranked[:size])
            values.append({"count": min(size, len(ranked)), "amount": round(amount, 2), "share": _ratio(amount, total)})
        return {f"cr{size}": value for size, value in zip((3, 5, 10), values)}

    return {
        "entity_count": len(totals),
        "unit_concentration": concentration(ranked_units, "units", units_total),
        "revenue_concentration": concentration(ranked_revenue, "revenue", revenue_total),
        "top_by_units": [{"name": name, "units": round(value["units"], 2),
                          "unit_share": _ratio(value["units"], units_total),
                          "revenue": round(value["revenue"], 2),
                          "revenue_share": _ratio(value["revenue"], revenue_total)}
                         for name, value in ranked_units[:10]],
    }


def _present(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _distribution(rows: list[dict], is_mapped: bool, value_fn, labels: list[str] | None = None,
                  include_empty: bool = False):
    mapped = [row for row in rows if _present(row.get("source"))]
    missing_rows = len(rows) - len(mapped)
    total_units = sum(row["units"] or 0.0 for row in rows)
    missing_units = sum(row["units"] or 0.0 for row in rows if not _present(row.get("source")))
    buckets: dict[str, dict[str, float]] = {}
    for row in mapped:
        label = value_fn(row["source"])
        label = label if label is not None else "未识别"
        entry = buckets.setdefault(str(label), {"rows": 0, "units": 0.0})
        entry["rows"] += 1
        entry["units"] += row["units"] or 0.0
    ordered = labels or sorted(buckets, key=str.casefold)
    for label in buckets:
        if label not in ordered:
            ordered.append(label)
    return {
        "buckets": [{"label": label, "rows": int(buckets.get(label, {}).get("rows", 0)),
                     "link_count": int(buckets.get(label, {}).get("rows", 0)),
                     "link_share": _ratio(buckets.get(label, {}).get("rows", 0), len(rows)),
                     "units": round(buckets.get(label, {}).get("units", 0.0), 2),
                     "unit_share": _ratio(buckets.get(label, {}).get("units", 0.0), total_units)}
                    for label in ordered if include_empty or label in buckets],
        "missing": {"rows": missing_rows, "link_count": missing_rows,
                    "link_share": _ratio(missing_rows, len(rows)), "units": round(missing_units, 2),
                    "unit_share": _ratio(missing_units, total_units)},
        "total_units": round(total_units, 2),
        "available": bool(is_mapped),
    }


def _numeric_distribution(rows: list[dict], role: str, role_mapped: bool, label_fn, labels,
                          include_empty: bool = True):
    prepared = [{**row, "source": row.get(role)} for row in rows]
    result = _distribution(prepared, role_mapped, label_fn, labels, include_empty)
    result["available"] = bool(role_mapped)
    return result


def _add_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, month = divmod(month_index, 12)
    month += 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def _price_bucket(value: float) -> str:
    if value < 0:
        return "未识别"
    if value < 5:
        return "0–<5"
    if value < 10:
        return "5–<10"
    if value < 40:
        lower = 10 + int((value - 10) // 5) * 5
        return f"{lower}–<{lower + 5}"
    if value < 50:
        return "40–<50"
    lower = 50 + int((value - 50) // 10) * 10
    return f"{lower}–<{lower + 10}"


def compute_metrics(source_rows: list[dict], mapping: dict, data_month: str | None = None,
                    as_of_date: str | None = None, currency: str | None = None) -> dict:
    """Compute sample-weighted summaries; missing values stay visible and unfilled."""
    rows = []
    for source in source_rows:
        raw = source.get("raw") if isinstance(source.get("raw"), dict) else source

        def get(role):
            header = mapping.get(role)
            return raw.get(header) if header else None

        units = number(get("monthly_sales"))
        if units is None or units < 0:
            units = None
        price = number(get("price"))
        source_revenue = number(get("revenue"))
        revenue = source_revenue if source_revenue is not None else (price * units if price is not None and units is not None else None)
        rows.append({
            "asin": get("asin"), "brand": get("brand"), "units": units,
            "revenue": revenue, "revenue_basis": "source" if source_revenue is not None else "estimated" if revenue is not None else None,
            "price": get("price"), "seller_country": get("seller_country"),
            "fulfillment": get("fulfillment"), "review_count": get("review_count"),
            "rating": get("rating"), "listing_date": get("listing_date"),
        })

    source_row_count = len(rows)
    if mapping.get("asin"):
        deduplicated = []
        asin_positions = {}
        for row in rows:
            key = _text(row.get("asin"))
            key = key.casefold() if key else None
            if not key or key not in asin_positions:
                if key:
                    asin_positions[key] = len(deduplicated)
                deduplicated.append(row)
                continue
            existing_index = asin_positions[key]
            existing = deduplicated[existing_index]
            if row["units"] is not None and (existing["units"] is None or row["units"] > existing["units"]):
                deduplicated[existing_index] = row
        rows = deduplicated
    duplicate_asins_removed = source_row_count - len(rows)
    unknown_sales_rows = sum(1 for row in rows if row["units"] is None)

    units_total = sum(row["units"] or 0.0 for row in rows)
    known_revenue = [row["revenue"] for row in rows if row["revenue"] is not None]
    revenue_total = sum(known_revenue)
    # Source revenue is used whenever present; price × sales estimates only the remaining rows.
    revenue_origin = "source" if any(row["revenue_basis"] == "source" for row in rows) else None
    estimate_present = any(row["revenue_basis"] == "estimated" for row in rows)
    if revenue_origin and estimate_present:
        revenue_origin = "mixed_source_and_estimate"
    elif not revenue_origin and estimate_present:
        revenue_origin = "estimated_price_times_sales"

    def category(value):
        return _text(value)

    def fulfillment(value):
        text = (_text(value) or "").upper().replace(" ", "")
        if not text:
            return None
        if "AMZ" in text or "AMAZON" in text:
            return "AMZ"
        if "FBA" in text:
            return "FBA"
        if "FBM" in text or "MERCHANT" in text:
            return "FBM"
        return _text(value)

    def review_band(value):
        value = number(value)
        if value is None:
            return None
        if value < 0: return "未识别"
        if value <= 100: return "0–100"
        if value <= 500: return "101–500"
        if value <= 1000: return "501–1,000"
        if value <= 5000: return "1,001–5,000"
        return "5,001+"

    def rating_band(value):
        value = number(value)
        if value is None: return None
        if value < 0 or value > 5: return "未识别"
        if value < 3.7: return "[0, 3.7)"
        if value < 4.0: return "[3.7, 4.0)"
        if value < 4.3: return "[4.0, 4.3)"
        if value < 4.7: return "[4.3, 4.7)"
        return "[4.7, 5]"

    month_text = _text(data_month) or ""
    month_match = re.fullmatch(r"(\d{4})[-/]?(\d{2})", month_text)
    as_of = None
    if month_match:
        year, month = int(month_match.group(1)), int(month_match.group(2))
        if 1 <= month <= 12:
            as_of = date(year, month, calendar.monthrange(year, month)[1])
    as_of = as_of or _date(as_of_date) or date.today()
    def age_band(value):
        listed = _date(value)
        if not listed: return None
        if listed > as_of:
            return "未识别"
        if _add_months(listed, 3) > as_of: return "[0, 3mo)"
        if _add_months(listed, 6) > as_of: return "[3, 6mo)"
        if _add_months(listed, 12) > as_of: return "[6, 12mo)"
        if _add_months(listed, 24) > as_of: return "[1, 2y)"
        return "[2y+]"

    price_values = [number(row["price"]) for row in rows]
    price_labels = sorted(
        {_price_bucket(value) for value in price_values if value is not None},
        key=lambda label: float(label.split("–", 1)[0]) if label != "未识别" else float("inf"),
    )
    if "未识别" not in price_labels:
        price_labels.append("未识别")
    def price_band(value):
        value = number(value)
        if value is None: return None
        return _price_bucket(value)

    exact_prices: dict[float, dict[str, float]] = {}
    for row in rows:
        price_value = number(row["price"])
        if price_value is None:
            continue
        exact = round(price_value, 2)
        entry = exact_prices.setdefault(exact, {"units": 0.0, "link_count": 0})
        entry["units"] += row["units"] or 0.0
        entry["link_count"] += 1

    def revenue_shares(key):
        totals: dict[str, float] = {}
        for row in rows:
            label = _text(row.get(key))
            if label and row["revenue"] is not None:
                totals[label] = totals.get(label, 0.0) + row["revenue"]
        ordered = sorted(totals.items(), key=lambda pair: (-pair[1], pair[0].casefold()))
        result = {}
        for size in (3, 5, 10):
            amount = sum(value for _, value in ordered[:size])
            result[f"cr{size}"] = {"count": min(size, len(ordered)), "amount": round(amount, 2),
                                   "share": _ratio(amount, revenue_total)}
        return result

    effective_revenue = revenue_total if known_revenue else 0.0
    seller_country_rows = [{**row, "source": row["seller_country"]} for row in rows]
    fulfillment_rows = [{**row, "source": row["fulfillment"]} for row in rows]
    exact_price_values = [{"price": price, "currency": _text(currency),
                           "units": round(value["units"], 2),
                           "link_count": int(value["link_count"])}
                          for price, value in sorted(exact_prices.items())]
    price_distribution = _numeric_distribution(rows, "price", bool(mapping.get("price")),
                                               price_band, price_labels + ["未识别"])
    price_distribution["currency"] = _text(currency)
    price_distribution["exact_prices"] = exact_price_values
    return {
        "sample_rows": source_row_count,
        "analysis_rows": len(rows),
        "duplicate_asins_removed": duplicate_asins_removed,
        "sample_units": round(units_total, 2),
        "unknown_sales_rows": unknown_sales_rows,
        "revenue": {"amount": round(revenue_total, 2) if known_revenue else None,
                    "basis": revenue_origin, "estimated": revenue_origin in {"estimated_price_times_sales", "mixed_source_and_estimate"},
                    "known_rows": len(known_revenue), "missing_rows": len(rows) - len(known_revenue)},
        "market_size": {"sample_monthly_sales": round(units_total, 2), "keyword_search_volume": None,
                        "keyword_search_volume_status": "not_collected_in_phase_1"},
        "competition": {
            "asin": _rank_shares(rows, "asin", units_total, effective_revenue),
            "brand": _rank_shares(rows, "brand", units_total, effective_revenue),
            "asin_revenue": revenue_shares("asin"),
            "brand_revenue": revenue_shares("brand"),
            "seller_country": _distribution(seller_country_rows, bool(mapping.get("seller_country")), category),
            "fulfillment": _distribution(fulfillment_rows, bool(mapping.get("fulfillment")), fulfillment),
            "review_count": _numeric_distribution(rows, "review_count", bool(mapping.get("review_count")), review_band,
                                                   ["0–100", "101–500", "501–1,000", "1,001–5,000", "5,001+", "未识别"]),
            "rating": _numeric_distribution(rows, "rating", bool(mapping.get("rating")), rating_band,
                                             ["[0, 3.7)", "[3.7, 4.0)", "[4.0, 4.3)", "[4.3, 4.7)", "[4.7, 5]", "未识别"]),
            "price": price_distribution,
            "listing_age": _numeric_distribution(rows, "listing_date", bool(mapping.get("listing_date")), age_band,
                                                  ["[0, 3mo)", "[3, 6mo)", "[6, 12mo)", "[1, 2y)", "[2y+]", "未识别"]),
        },
        "method": {"weight": "sampled_monthly_sales", "cr_basis": "sample_rows_only",
                   "price_bands": "fixed_currency_ranges", "price_currency": _text(currency),
                   "listing_age_as_of": as_of.isoformat(),
                   "missing_values": "kept_separate_and_not_imputed"},
        "mapping_status": {role: bool(mapping.get(role)) for role in mapping},
    }
