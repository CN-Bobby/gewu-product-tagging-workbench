import json

from backend.product_archive.insights import merge_keyword_insights, merge_voc_insights


def _term(keyword, relevance="A", searches=0, purchase=0.0, **extra):
    return {
        "keyword": keyword,
        "relevance": relevance,
        "searches": searches,
        "purchaseRate": purchase,
        "bid": "1.25",
        "top3ClickingRate": 0.2,
        "top3ConversionRate": 0.1,
        "traffic_type": "core",
        "is_noise": False,
        **extra,
    }


def test_keyword_filter_sort_dedupe_medians_and_non_additive_signals():
    archive = {"overview": {"representative_keywords": ""}, "updated_at": "2026-10-03T00:00:00Z"}
    runs = [
        {"run_id": "run-b", "data_month": "2026-09", "keywords": [
            _term("  ALPHA ", searches="100", purchase="0.1", bid="2.5"),
            _term("beta", relevance="B", searches="900", purchase="0.5"),
            _term("noise", searches=5000, is_noise=True),
            _term("wrong", searches=5000, traffic_type="wrong_category"),
            _term("", searches=5000),
            _term("bad metric", searches="-2"),
            _term("not numeric", searches="many"),
            _term("C relevance", relevance="C", searches=8000),
        ]},
        {"run_id": "run-a", "data_month": "2026-08", "keywords": [
            _term("Zulu", searches=100, purchase=0.1, bid=4),
            _term("Alpha", searches=100, purchase=0.1, bid=8),
            _term("delta", searches=40, purchase=0.2, bid=2),
        ]},
    ]

    result = merge_keyword_insights(archive, runs)
    insight = result["integrations"]["keyword_research"]
    terms = insight["representative_terms"]

    assert [item["keyword"] for item in terms] == ["Alpha", "Zulu", "delta", "beta"]
    assert terms[0] == {
        "keyword": "Alpha", "searches": 100.0, "bid": 8.0,
        "top3ClickingRate": 0.2, "top3ConversionRate": 0.1,
        "purchaseRate": 0.1, "relevance": "A", "traffic_type": "core",
        "data_month": "2026-08", "source_run_id": "run-a",
    }
    assert insight["summary"] == {
        "representative_term_count": 4,
        "searches_median": 100.0,
        "bid_median": 3.0,
        "top3_click_median": 0.2,
        "top3_conversion_median": 0.1,
        "non_additive": True,
    }
    # Duplicate Alpha searches are retained once, never summed into 200.
    assert insight["summary"]["searches_median"] != 200
    assert result["overview"]["representative_keywords"] == ""
    suggestion = result["overview_suggestions"]["representative_keywords"][0]
    assert suggestion["text"] == "Alpha\nZulu\ndelta\nbeta"
    assert suggestion["source_type"] == "keyword_research"
    assert suggestion["source_ids"] == ["run-a", "run-b"]
    assert "integrations" not in archive


def test_keyword_limit_and_manual_overview_protection():
    rows = [_term(f"term-{index:02}", searches=100 - index) for index in range(12)]
    archive = {
        "overview": {"representative_keywords": "my own words"},
        "overview_sources": {},
    }
    first = merge_keyword_insights(archive, [
        {"run_id": "kw-1", "updated_at": "2026-10-01T00:00:00Z", "keywords": rows},
    ])
    assert len(first["integrations"]["keyword_research"]["representative_terms"]) == 10
    assert first["overview"]["representative_keywords"] == "my own words"
    assert first["overview_suggestions"]["representative_keywords"]

    generated_base = {"overview": {"representative_keywords": "old generated"},
                      "overview_sources": {"representative_keywords": {
                          "source_type": "keyword_research", "source_ids": ["old"],
                      }}}
    refreshed = merge_keyword_insights(generated_base, [
        {"run_id": "kw-2", "updated_at": "2026-10-02T00:00:00Z",
         "keywords": [_term("new generated", searches=3)]},
    ])
    assert refreshed["overview"]["representative_keywords"] == "old generated"
    assert refreshed["overview_suggestions"]["representative_keywords"][0]["text"] == "new generated"


def test_voc_uses_top_formal_segments_and_keeps_scope_provenance_without_raw_data():
    private_review = "PRIVATE REVIEW TEXT 9QZX"
    private_asin = "B012345678"
    archive = {
        "overview": {"product_function": "", "target_audience": ""},
        "updated_at": "2026-10-03T00:00:00Z",
    }
    run = {"run_id": "voc-7", "revision": 3, "reviews_sha256": "f" * 64,
           "updated_at": "2026-10-02T12:00:00Z", "workspace_id": "workspace-1",
           "source_archive_id": "archive-1", "source_dataset_id": "dataset-1"}
    segments = [
        {"segment_id": "low", "status": "formal", "metrics": {"review_count": 2},
         "jtbd_statement": "Low ranked function", "persona_summary": "Low ranked audience"},
        {"segment_id": "top", "status": "formal", "metrics": {"review_count": 9},
         "jtbd_statement": "Top function", "persona_summary": "Top audience",
         "review_text": private_review, "asin": private_asin},
        {"segment_id": "second", "status": "formal", "metrics": {"review_count": 6},
         "jtbd_statement": "Second function", "persona_summary": "Second audience"},
        {"segment_id": "third", "status": "formal", "metrics": {"review_count": 4},
         "jtbd_statement": "Third function", "persona_summary": "Third audience"},
        {"segment_id": "exploratory", "status": "exploratory", "metrics": {"review_count": 1000},
         "jtbd_statement": "Must not use", "persona_summary": "Must not use either"},
    ]
    analysis = {"segments": segments, "validation": {"all_green": True},
                "review_text": private_review, "asins": [private_asin]}
    source_context = {
        "review_count": 28, "requested_asin_count": 5, "comment_asin_count": 3,
        "failed_asin_count": 1, "completed_asin_count": 4,
        "empty_comment_asin_count": 1, "source_job_status": "partial",
        "analysis_scope": "Only uploaded workbook comments", "failed_asins": [private_asin],
    }

    result = merge_voc_insights(archive, run, analysis, source_context)
    integration = result["integrations"]["amazon_voc"]
    assert integration == {
        "updated_at": "2026-10-02T12:00:00Z",
        "run_id": "voc-7",
        "revision": 3,
        "reviews_sha256": "f" * 64,
        "review_count": 28,
        "requested_asin_count": 5,
        "comment_asin_count": 3,
        "failed_asin_count": 1,
        "quality_all_green": True,
        "segments_used": [
            {"segment_id": "top", "review_count": 9},
            {"segment_id": "second", "review_count": 6},
            {"segment_id": "third", "review_count": 4},
        ],
        "product_function_suggestion": "Top function；Second function；Third function",
        "target_audience_suggestion": "Top audience；Second audience；Third audience",
        "analysis_scope": {
            "analysis_scope": "Only uploaded workbook comments",
            "source_job_status": "partial",
            "completed_asin_count": 4,
            "empty_comment_asin_count": 1,
        },
    }
    assert result["overview"]["product_function"] == ""
    assert result["overview_suggestions"]["product_function"][0]["text"] == integration["product_function_suggestion"]
    assert result["overview_suggestions"]["target_audience"][0]["text"] == integration["target_audience_suggestion"]
    serialized = json.dumps(result, ensure_ascii=False)
    assert private_review not in serialized
    assert private_asin not in serialized
    assert archive["overview"]["product_function"] == ""


def test_voc_preserves_manual_values_refreshes_owned_fields_and_bounds_deduplicated_suggestions():
    manual = {
        "overview": {"product_function": "Manual function", "target_audience": "Manual audience"},
        "overview_sources": {},
    }
    run = {"run_id": "voc-new", "revision": 4}
    analysis = {"validation": {"all_green": False}, "segments": [
        {"segment_id": "b", "status": "formal", "metrics": {"review_count": 8},
         "jtbd_statement": "Same task", "persona_summary": "Audience " * 80},
        {"segment_id": "a", "status": "formal", "metrics": {"review_count": 8},
         "jtbd_statement": " same   task ", "persona_summary": "Distinct audience"},
        {"segment_id": "c", "status": "formal", "metrics": {"review_count": 7},
         "jtbd_statement": "Another task", "persona_summary": "Third audience"},
    ]}
    preserved = merge_voc_insights(manual, run, analysis, {})
    assert preserved["overview"]["product_function"] == "Manual function"
    assert preserved["overview"]["target_audience"] == "Manual audience"
    assert preserved["integrations"]["amazon_voc"]["product_function_suggestion"] == "same task；Another task"
    assert len(preserved["integrations"]["amazon_voc"]["target_audience_suggestion"]) <= 900
    assert preserved["integrations"]["amazon_voc"]["quality_all_green"] is False

    generated = {
        "overview": {"product_function": "Old function", "target_audience": "Old audience"},
        "overview_sources": {
            "product_function": {"source_type": "amazon_voc", "source_ids": ["voc-old"]},
            "target_audience": {"source_type": "amazon_voc", "source_ids": ["voc-old"]},
        },
    }
    refreshed = merge_voc_insights(
        generated, {"run_id": "voc-new", "revision": 4},
        {"validation": {"all_green": True}, "segments": [{
            "segment_id": "fresh", "status": "formal", "metrics": {"review_count": 3},
            "jtbd_statement": "Fresh function", "persona_summary": "Fresh audience",
        }]}, {},
    )
    assert refreshed["overview"]["product_function"] == "Old function"
    assert refreshed["overview"]["target_audience"] == "Old audience"
    assert refreshed["overview_suggestions"]["product_function"][0]["text"] == "Fresh function"
    assert refreshed["overview_suggestions"]["target_audience"][0]["text"] == "Fresh audience"


def test_voc_missing_legacy_review_scope_is_unknown_not_zero():
    result = merge_voc_insights(
        {"overview": {}}, {"run_id": "legacy-run"},
        {"segments": [], "validation": {"all_green": True}}, {},
    )
    insight = result["integrations"]["amazon_voc"]
    assert insight["review_count"] is None
    assert insight["requested_asin_count"] is None
    assert insight["comment_asin_count"] is None
    assert insight["failed_asin_count"] is None
