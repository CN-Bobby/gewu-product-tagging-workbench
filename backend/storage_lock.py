"""Process-owned lock for packaged startup, backup and offline restoration."""
from contextlib import contextmanager
from pathlib import Path
import os


@contextmanager
def exclusive_storage(directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    with (directory/'runtime.lock').open('a+b') as handle:
        handle.seek(0,2)
        if handle.tell()==0:handle.write(b'0');handle.flush()
        handle.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError('数据目录正在使用，停止工作台后再升级或恢复。') from error
        try:yield
        finally:
            handle.seek(0)
            if os.name=='nt':msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(handle,fcntl.LOCK_UN)
