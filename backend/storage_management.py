"""Count authoritative data separately; clear only explicitly disposable caches."""
import os
from pathlib import Path
from sqlalchemy import select,delete,func
from .db import *

SOFT_LIMIT=10*1024**3

def folder_bytes(path):
    if not path.exists():return 0
    return sum(p.stat().st_size for p in path.rglob('*') if p.is_file() and not p.is_symlink())


def usage():
    cache=Path(os.environ.get('TAG_CACHE',STORAGE/'cache')).resolve()
    with Session() as s:
        counts={cls.__tablename__:s.scalar(select(func.count()).select_from(cls)) for cls in (VisionCache,TextCache)}
        estimated={cls.__tablename__:s.scalar(select(func.sum(func.length(cls.content)))) or 0 for cls in (VisionCache,TextCache)}
    disposable=folder_bytes(cache) if cache_engine is not None else sum(estimated.values())
    return {'data_path':str(STORAGE),'cache_path':str(cache),'soft_limit_bytes':SOFT_LIMIT,
            'cache_bytes':disposable,'warning':disposable>=SOFT_LIMIT*.8,'over_limit':disposable>SOFT_LIMIT,
            'cache_records':counts,'categories':{'originals':folder_bytes(STORAGE/'originals'),
            'original_images':folder_bytes(STORAGE/'assets'),'sessions_and_credentials':folder_bytes(STORAGE/'datasets'),
            'backups':folder_bytes(STORAGE/'backups'),'database':(STORAGE/'workbench.db').stat().st_size if (STORAGE/'workbench.db').exists() else 0,
            'exports':folder_bytes(STORAGE/'exports'),'disposable_cache':disposable},
            'note':'数据库体积包含原始记录与权威结果；清理缓存不删除原表、原图、结果、导出或会话。'}


def clear():
    from . import batch,draft
    if batch.active:raise ValueError('请先暂停批处理并等待保存完成，再清理缓存')
    with draft.WRITE_LOCK,Session.begin() as s:
        s.execute(delete(VisionCache));s.execute(delete(TextCache))
    if cache_engine is not None:
        with cache_engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:connection.exec_driver_sql('VACUUM')
    # No recursive file deletion: only cache table content is disposable here.
    return usage()
