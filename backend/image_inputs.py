"""Stable image input selection for tagging and result reuse.

The current production data contract is one spreadsheet main image per product.
Supplemental ASIN images may be attached for manual review, but they must not
silently change the automatic tagging input.
"""


def local_assets(product):
    return [asset for asset in product.get('assets', []) if asset.get('path')]


def primary_asset(product):
    assets = local_assets(product)
    if not assets:
        raise ValueError('没有可用本地主图')
    # Legacy rows may mark every image as main.  Prefer the spreadsheet image,
    # then an explicitly main image, and finally the oldest stable asset.
    return min(
        assets,
        key=lambda asset: (
            0 if asset.get('source') == 'excel' else 1,
            0 if asset.get('image_type') == 'main' else 1,
            asset.get('created_at') or '',
            asset.get('id') or '',
        ),
    )


def selection(product):
    chosen = primary_asset(product)
    return {
        'asset': chosen,
        'input_policy': 'single_main_image_v1',
        'available_local_images': len(local_assets(product)),
        'supplemental_images_ignored': max(0, len(local_assets(product)) - 1),
    }
