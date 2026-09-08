"""Explicit rollback utility. Refuses live service; backs up current DB first."""
import argparse,socket,sqlite3,sys
from pathlib import Path
from datetime import datetime
root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description='恢复工作台数据库（请先暂停任务并停止服务）')
parser.add_argument('backup',type=Path);args=parser.parse_args()
source=args.backup.resolve();folder=(root/'storage/backups').resolve()
if not source.is_relative_to(folder) or not source.is_file():parser.error('请选择本项目 storage/backups 中的备份文件')
with socket.socket() as probe:
    if probe.connect_ex(('127.0.0.1',8765))==0:parser.error('请先停止8765端口的工作台服务')
with sqlite3.connect('file:'+source.as_posix()+'?mode=ro',uri=True) as backup:
    if backup.execute('pragma integrity_check').fetchone()[0]!='ok':parser.error('备份完整性检查失败')
    with sqlite3.connect(root/'storage/workbench.db') as current:
        target=folder/('before-restore-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.db')
        with sqlite3.connect(target) as dst:current.backup(dst)
        backup.backup(current)
print('数据库已恢复。再次启动新版应用会从保留的旧表重新建立草稿；原始Excel和图片文件不变。')
