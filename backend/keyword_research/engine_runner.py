# -*- coding: utf-8 -*-
"""以子进程方式运行内置引擎脚本，并按需生成报告模板。

源码来自 sellersprite-keyword-app-rs 的 app/core/engine.py，但执行方式改为子进程：
原实现用 runpy 在同进程内执行脚本并改写全局 sys.argv/sys.path，工作台的作业跑在
线程里，那种写法不可重入、非线程安全。脚本本身就是 CLI 形状，改成子进程后既隔离了
全局状态，也能在取消时直接终止子进程。

解释器取 sys.executable，与 harness.py 起 MCP 插件子进程的既有做法一致；打包后它指向
runtime/pythonw.exe。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

ENGINE_ROOT = Path(__file__).resolve().parent / 'engine'
SCRIPTS_DIR = ENGINE_ROOT / 'scripts'

# 引擎资产原样搬运时带过一份报告模板 xlsx，但它不进版本库（仓库审计禁止 .xlsx 入库），
# 改为首次使用时按需生成，所以这里只要求生成器本身在。
REQUIRED_SCRIPTS = (
    'aggregate_batches.py', 'normalize_keywords.py',
    'aggregate_asin_keyword_matrix.py', 'classify_keywords.py',
    'validate_labels.py', 'select_trend_keywords.py', 'analyze_trends.py',
    'validate_analysis.py', 'build_excel_report.py', 'validate_report.py',
    'build_template.py',
)

OnOutput = Optional[Callable[[str], None]]


class EngineError(Exception):
    def __init__(self, script: str, workdir: Path, returncode: int, detail: str) -> None:
        self.script = script
        self.workdir = workdir
        self.returncode = returncode
        self.detail = detail
        super().__init__(f'引擎脚本 {script} 失败（exit {returncode}）: {detail[-800:]}')


class EngineCancelled(Exception):
    """脚本因取消被终止。"""


def engine_available() -> bool:
    missing = [name for name in REQUIRED_SCRIPTS if not (SCRIPTS_DIR / name).is_file()]
    return not missing


def asset_digest() -> dict:
    """引擎资产的冻结哈希，写进运行记录作为产物出处。"""
    digest = hashlib.sha256()
    files = sorted(path for path in ENGINE_ROOT.rglob('*') if path.is_file())
    for path in files:
        digest.update(path.relative_to(ENGINE_ROOT).as_posix().encode())
        digest.update(b'\0' + path.read_bytes() + b'\0')
    return {'sha256': digest.hexdigest(), 'files': len(files)}


def template_path() -> Path:
    """报告模板路径：放在实例数据目录里，缺了就现场生成。

    不写程序目录：运行时往程序目录落文件会破坏「程序目录运行时零写入」这条不变量。
    build_template.py 能重建出同名 13 Sheet、取值一致的模板，所以模板不必入库。
    """
    from ..db import STORAGE
    target = STORAGE / 'keyword-research' / 'report-template' / 'keyword_market_report_template.xlsx'
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    _spawn('build_template.py', [sys.executable, str(SCRIPTS_DIR / 'build_template.py'),
                                 '--out', str(target)], target.parent)
    if not target.is_file():
        raise EngineError('build_template.py', target.parent, 1, '模板生成后仍不存在')
    return target


def template_digest() -> dict:
    path = template_path()
    data = path.read_bytes()
    return {'name': path.name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def _child_env() -> dict:
    # 引擎脚本会 import 同目录的兄弟模块，靠的是解释器自带的 sys.path[0]（脚本所在目录），
    # 不需要像原实现那样手工插 sys.path。
    env = dict(os.environ)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    return env


def _spawn(name: str, command: list[str], workdir: Path, on_output: OnOutput = None,
           timeout: float = 600.0, cancel: Optional[threading.Event] = None) -> str:
    """跑一个子进程并把输出逐行交给 on_output。cwd 指向运行目录而非程序目录。"""
    process = subprocess.Popen(
        command, cwd=str(workdir), env=_child_env(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding='utf-8', errors='replace', bufsize=1,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    lines: list[str] = []

    def drain() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip('\n')
            lines.append(line)
            if on_output is not None:
                try:
                    on_output(line)
                except Exception:  # noqa: BLE001
                    pass

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        reader.join(timeout=10)
        raise EngineError(name, workdir, -1, f'脚本超过 {timeout} 秒未结束，已终止')
    # 进程已退出，stdout 必然 EOF；读者线程必须真正收工，否则后到的输出会排到调用方的
    # 终态事件之后，把持久化顺序弄坏。
    reader.join(timeout=30)
    if reader.is_alive():
        raise EngineError(name, workdir, code, '脚本退出后输出仍未读完')
    if cancel is not None and cancel.is_set():
        raise EngineCancelled(f'引擎脚本 {name} 因取消而终止')
    output = '\n'.join(lines)
    if code != 0:
        raise EngineError(name, workdir, code, output)
    return output


def run_script(name: str, workdir: Path | str, *extra_args: str,
               on_output: OnOutput = None, timeout: float = 600.0,
               cancel: Optional[threading.Event] = None) -> str:
    """运行一个引擎脚本（--workdir 形状），返回其完整输出。"""
    script = SCRIPTS_DIR / name
    if not script.is_file():
        raise FileNotFoundError(f'引擎脚本不存在: {script}')
    workdir = Path(workdir)
    return _spawn(name, [sys.executable, str(script), '--workdir', str(workdir), *extra_args],
                  workdir, on_output, timeout, cancel)
