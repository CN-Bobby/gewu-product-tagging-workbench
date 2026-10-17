import html
import re

from backend.amazon_voc.runtime.scripts.build_html import bilingual, evidence_links


def test_report_evidence_quote_does_not_embed_a_complete_long_review():
    review = "Bought this for a birthday party with a mermaid theme; it was cute and true to size."

    rendered = evidence_links([{"event_id": "EV-1", "quote": review, "quote_omitted": False}])
    attribute = re.search(r'data-quote="([^"]*)"', rendered)

    assert attribute is not None
    quote = html.unescape(attribute.group(1))
    assert len(quote) <= 39
    assert review not in quote


def test_bilingual_report_copy_does_not_name_unavailable_external_sources():
    rendered = bilingual(
        "未联网、未引用 Alexa/Customer Say 或产品页。",
        "No Alexa/Customer Say or product pages were used.",
    )

    assert "Alexa" not in rendered
    assert "Customer Say" not in rendered
    assert "其他未提供的数据源" in rendered
    assert "other unprovided sources" in rendered
