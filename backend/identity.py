"""Stable product identity selection shared by preview, import, and remapping."""


def meaningful(value):
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip()
    return bool(text) and text != '0'


def choose(fields, row):
    for source in ('asin', 'sku', 'parent_asin'):
        value = (fields or {}).get(source)
        if meaningful(value):
            return str(value).strip(), source
    return f'ROW-{row}', 'row'
