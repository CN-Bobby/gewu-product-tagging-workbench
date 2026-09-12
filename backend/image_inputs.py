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


POLICIES=('single_main_image_v1','single_main_with_quadrant_crops_v1','screenshot_grid_crops_v1','main_plus_supplemental_v1')

def selection(product,policy='single_main_image_v1'):
    if policy not in POLICIES:raise ValueError('未知图片输入策略')
    chosen = primary_asset(product)
    assets=local_assets(product)
    selected=[chosen]
    if policy=='main_plus_supplemental_v1':selected=assets[:4]
    return {
        'asset': chosen,
        'assets':selected,
        'input_policy': policy,
        'crop_policy':'quadrants_v1' if policy=='single_main_with_quadrant_crops_v1' else 'adaptive_grid_v1' if policy=='screenshot_grid_crops_v1' else None,
        'available_local_images': len(assets),
        'supplemental_images_ignored': max(0, len(assets) - len(selected)),
    }
