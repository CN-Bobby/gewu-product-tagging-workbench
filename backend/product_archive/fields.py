"""Field roles used by the first Product Archive phase."""

FIELD_ROLES = {
    "asin": {"label": "ASIN", "aliases": ["ASIN", "asin", "ASIN码", "ASIN Code"]},
    "url": {"label": "商品详情链接", "aliases": ["商品详情链接", "详情链接", "商品链接", "Listing URL", "Product URL", "URL", "链接"]},
    "product_name": {"label": "产品名称", "aliases": ["商品名称", "产品名称", "商品标题", "标题", "Product Title", "Title"]},
    "brand": {"label": "品牌", "aliases": ["品牌", "Brand", "品牌名称"]},
    "monthly_sales": {"label": "月销量", "aliases": ["月销量", "预估月销量", "Monthly Sales", "Sales", "销量"]},
    "revenue": {"label": "销售额", "aliases": ["月销售额", "销售额", "Revenue", "Monthly Revenue", "Sales Revenue"]},
    "price": {"label": "价格", "aliases": ["价格", "价格($)", "Price", "售价"]},
    "seller_country": {"label": "卖家国家/地区", "aliases": ["卖家国家", "卖家国家/地区", "卖家所在地", "卖家所属地", "Seller Country", "Seller Location"]},
    "fulfillment": {"label": "配送方式", "aliases": ["配送方式", "配送类型", "履约方式", "Fulfillment", "Fulfillment Type"]},
    "review_count": {"label": "评分数", "aliases": ["评分数", "评论数", "Review Count", "Reviews"]},
    "rating": {"label": "评分星级", "aliases": ["评分", "星级", "Rating", "Star Rating"]},
    "listing_date": {"label": "上架时间", "aliases": ["上架时间", "上架日期", "首次上架时间", "Listing Date", "Date First Available"]},
}

OVERVIEW_FIELDS = {
    "product_name": "产品名称",
    "representative_keywords": "代表关键词",
    "product_function": "产品功能",
    "target_audience": "人群受众",
    "buying_motives": "需求与动机",
    "cultural_context": "文化背景",
    "seasonality": "季节性",
}


def default_mapping(headers, imported_mapping=None):
    headers = [str(value) for value in (headers or [])]
    existing = imported_mapping or {}
    result = {}
    for role, spec in FIELD_ROLES.items():
        candidate = existing.get(role)
        if not candidate:
            for key in (role, {"monthly_sales": "sales", "review_count": "reviews"}.get(role, "")):
                if key and existing.get(key):
                    candidate = existing[key]
                    break
        if candidate not in headers:
            candidate = next((header for header in headers if header.casefold() in {
                str(alias).casefold() for alias in spec["aliases"]
            }), None)
        result[role] = candidate
    return result
