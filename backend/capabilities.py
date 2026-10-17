"""Public release capabilities."""
import os
from .build_info import VERSION


def _enabled(name):
    return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on')


def visual_tagging_enabled():
    # Automatic visual tagging is a supported beta capability.  A deployment may
    # still disable it explicitly when its configured provider has no vision model.
    return not _enabled('TAG_DISABLE_VISUAL_TAGGING')


def require_visual_tagging():
    if not visual_tagging_enabled():
        raise ValueError(f'{VERSION} 当前部署已关闭自动视觉打标；请启用视觉模型后重试')


def public():
    return {
        'visual_tagging': visual_tagging_enabled(),
        'visual_tagging_status': 'available' if visual_tagging_enabled() else 'disabled_by_deployment',
        'online_updates': True,
    }
