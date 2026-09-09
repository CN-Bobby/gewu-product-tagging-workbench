"""Release capability gates. Experimental features require an explicit local opt-in."""
import os
from .build_info import VERSION


def _enabled(name):
    return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on')


def visual_tagging_enabled():
    return _enabled('TAG_ENABLE_VISUAL_TAGGING')


def require_visual_tagging():
    if not visual_tagging_enabled():
        raise ValueError(f'{VERSION} 暂未开放自动视觉打标；可继续查看高清原图、核对已有结果或人工修正')


def public():
    return {
        'visual_tagging': visual_tagging_enabled(),
        'visual_tagging_status': 'internal_research' if visual_tagging_enabled() else 'disabled_for_beta',
        'online_updates': False,
    }
