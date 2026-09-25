"""Idempotent local parameter and live-progress panel. It never schedules MCP."""
from __future__ import annotations

import hashlib
import html
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from .db import utc_now
from .engine import DEFAULT_POLICY, list_runs, status
from .ledger import (delete_preset, get_profile, launch_instruction, list_presets,
                     save_preset, save_profile)

PANEL_APP = "xunlongjue-panel"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_SPAN = 11


def _workspace_id(workspace: str) -> str:
    return hashlib.sha256(os.path.normcase(os.path.abspath(workspace)).encode("utf-8")).hexdigest()


def _state_path(workspace: str) -> str:
    return os.path.join(os.path.abspath(workspace), "panel.json")


def _load_state(workspace: str) -> dict:
    try:
        with open(_state_path(workspace), "r", encoding="utf-8-sig") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(workspace: str, value: dict) -> None:
    os.makedirs(workspace, exist_ok=True)
    target = _state_path(workspace)
    with open(target + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    os.replace(target + ".tmp", target)


def _request_json(url: str, *, method: str = "GET", token: str = "", timeout: float = .8) -> dict:
    request = urllib.request.Request(url, method=method)
    if token:
        request.add_header("X-XLJ-Panel-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, urllib.error.URLError):
        return {}


def _health(port: int) -> dict:
    return _request_json(f"http://{DEFAULT_HOST}:{int(port)}/health")


def panel_status(workspace: str) -> dict:
    workspace = os.path.abspath(workspace)
    expected, state = _workspace_id(workspace), _load_state(workspace)
    ports = ([int(state["port"])] if state.get("port") else [])
    ports.extend(port for port in range(DEFAULT_PORT, DEFAULT_PORT + PORT_SPAN) if port not in ports)
    for port in ports:
        health = _health(port)
        if health.get("app") == PANEL_APP and health.get("workspace_id") == expected:
            return {"ok": True, "running": True, "action": "status",
                    "url": f"http://{DEFAULT_HOST}:{port}/", "port": port,
                    "pid": health.get("pid"), "version": health.get("version"),
                    "started_at": health.get("started_at")}
    return {"ok": True, "running": False, "action": "status", "url": "",
            "port": None, "version": __version__}


def _port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, int(port))); return True
        except OSError:
            return False


def _spawn_command(workspace: str, host: str, port: int, token: str) -> list[str]:
    args = ["panel", "serve", "--workspace", os.path.abspath(workspace),
            "--host", host, "--port", str(port), "--token", token]
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    entry = os.path.abspath(sys.argv[0])
    if not os.path.isfile(entry) or not entry.lower().endswith(".py"):
        entry = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "xlj.py"))
    return [sys.executable, entry, *args]


def open_panel(workspace: str, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
               open_browser: bool = True, timeout: float = 8.0) -> dict:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("面板只允许绑定本机 127.0.0.1")
    existing = panel_status(workspace)
    if existing["running"]:
        if open_browser: webbrowser.open(existing["url"])
        existing["action"] = "already_running"
        return existing
    chosen = next((candidate for candidate in range(int(port), int(port) + PORT_SPAN)
                   if _port_available(DEFAULT_HOST, candidate)), None)
    if chosen is None:
        raise RuntimeError(f"端口 {port}-{int(port)+PORT_SPAN-1} 均不可用")
    token = secrets.token_urlsafe(24)
    os.makedirs(workspace, exist_ok=True)
    log_path = os.path.join(os.path.abspath(workspace), "panel.log")
    kwargs = {"stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) |
                                   getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        kwargs["start_new_session"] = True
    with open(log_path, "a", encoding="utf-8") as log:
        process = subprocess.Popen(_spawn_command(workspace, DEFAULT_HOST, chosen, token),
                                   stdout=log, stderr=log, **kwargs)
    deadline, health = time.monotonic() + max(1.0, float(timeout)), {}
    while time.monotonic() < deadline:
        health = _health(chosen)
        if health.get("app") == PANEL_APP and health.get("workspace_id") == _workspace_id(workspace): break
        if process.poll() is not None:
            raise RuntimeError(f"面板进程启动失败，退出码 {process.returncode}；日志: {log_path}")
        time.sleep(.2)
    else:
        try: process.terminate()
        except OSError: pass
        raise RuntimeError(f"面板在 {timeout:g} 秒内未就绪；日志: {log_path}")
    threading.Thread(target=process.wait, daemon=True).start()
    url = f"http://{DEFAULT_HOST}:{chosen}/"
    if open_browser: webbrowser.open(url)
    return {"ok": True, "running": True, "action": "started", "url": url,
            "port": chosen, "pid": health.get("pid"), "version": health.get("version"), "log": log_path}


def stop_panel(workspace: str, timeout: float = 5.0) -> dict:
    current = panel_status(workspace)
    if not current["running"]:
        return {"ok": True, "running": False, "action": "not_running"}
    token = str(_load_state(workspace).get("token") or "")
    if not token: raise RuntimeError("找不到面板控制令牌；请关闭对应 xlj.exe 进程")
    response = _request_json(current["url"] + "shutdown", method="POST", token=token)
    if not response.get("ok"): raise RuntimeError("面板拒绝关闭请求")
    deadline = time.monotonic() + max(1.0, float(timeout))
    while time.monotonic() < deadline and panel_status(workspace)["running"]: time.sleep(.1)
    return {"ok": True, "running": False, "action": "stopped", "url": current["url"], "pid": current.get("pid")}


def _script_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


def _page(workspace: str, token: str) -> bytes:
    profile, presets = get_profile(workspace), list_presets(workspace)
    boot = {"version": __version__, "csrf": token, "profile": profile,
            "presets": presets, "policy": DEFAULT_POLICY}
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>寻龙诀任务台</title><style>
:root{{--ink:#15221e;--muted:#64736d;--paper:#f3f1e9;--panel:#fffef8;--line:#d9ddd5;--green:#0f6248;--lime:#c8ed71;--amber:#c47b20;--red:#a43c33}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:14px/1.5 "Segoe UI","Microsoft YaHei",sans-serif}}body:before{{content:"";position:fixed;inset:0;pointer-events:none;background:linear-gradient(90deg,#00000005 1px,transparent 1px),linear-gradient(#00000005 1px,transparent 1px);background-size:32px 32px}}
.shell{{position:relative;max-width:1500px;margin:auto;padding:28px}}header{{display:flex;justify-content:space-between;align-items:end;border-bottom:2px solid var(--ink);padding-bottom:18px}}h1{{font:700 34px/1 Georgia,"Microsoft YaHei";margin:0}}.eyebrow{{letter-spacing:.15em;text-transform:uppercase;color:var(--green);font-weight:700}}.version{{font-family:Consolas;background:var(--ink);color:#fff;padding:7px 10px}}
.grid{{display:grid;grid-template-columns:minmax(420px,.9fr) minmax(520px,1.35fr);gap:18px;margin-top:18px}}.card{{background:var(--panel);border:1px solid var(--line);box-shadow:4px 4px 0 #cfd4ca;padding:20px}}h2{{font:700 20px Georgia,"Microsoft YaHei";margin:0 0 4px}}.hint{{color:var(--muted);margin:0 0 18px}}
.form-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}}label{{display:grid;gap:5px;color:var(--muted);font-size:12px;font-weight:650}}label.wide{{grid-column:1/-1}}input,select,textarea,button{{font:inherit}}input,select,textarea{{width:100%;background:#fff;border:1px solid #bfc8c0;padding:9px 10px;color:var(--ink)}}input:focus,select:focus,textarea:focus{{outline:2px solid var(--lime);border-color:var(--green)}}button{{border:1px solid var(--ink);background:var(--ink);color:#fff;padding:10px 14px;font-weight:700;cursor:pointer}}button.alt{{background:transparent;color:var(--ink)}}button.danger{{border-color:var(--red);color:var(--red);background:transparent}}.actions{{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}}#instruction{{min-height:150px;font-family:Consolas,"Microsoft YaHei";line-height:1.6;background:#f7f8f2}}
.presets{{margin-top:18px;border-top:1px solid var(--line);padding-top:16px}}.preset-row{{display:grid;grid-template-columns:1fr auto;gap:8px;margin-top:8px}}.preset-meta{{font-size:12px;color:var(--muted)}}
.run-list{{display:grid;gap:10px}}.run{{border:1px solid var(--line);padding:14px;background:#fff}}.run-head{{display:flex;justify-content:space-between;gap:12px;align-items:start}}.run-title{{font-weight:800;font-size:16px}}.mono{{font-family:Consolas;font-size:12px;color:var(--muted)}}.pill{{font:700 11px Consolas;padding:4px 7px;background:#e8eee9;color:var(--green)}}.pill.done{{background:#dff0b7}}.pill.warn{{background:#f7e5c5;color:#87500c}}
.bar{{height:9px;background:#e7e9e4;margin:12px 0 8px;overflow:hidden}}.bar i{{display:block;height:100%;background:linear-gradient(90deg,var(--green),#55a666);transition:width .35s}}.metrics{{display:grid;grid-template-columns:repeat(5,1fr);gap:7px}}.metric{{background:#f3f5ef;padding:7px}}.metric b{{display:block;font:700 17px Georgia}}.metric small{{color:var(--muted)}}.batch{{margin-top:9px;padding:9px;border-left:3px solid var(--amber);background:#fff8eb;font-size:12px}}.empty{{padding:50px;text-align:center;color:var(--muted);border:1px dashed #bfc8c0}}
.toast{{position:fixed;right:22px;bottom:22px;background:var(--ink);color:#fff;padding:12px 16px;display:none;z-index:5}}@media(max-width:980px){{.grid{{grid-template-columns:1fr}}.metrics{{grid-template-columns:repeat(3,1fr)}}}}@media(max-width:600px){{.shell{{padding:16px}}.form-grid{{grid-template-columns:1fr}}.metrics{{grid-template-columns:repeat(2,1fr)}}}}
</style></head><body><div class="shell"><header><div><div class="eyebrow">Local scan operations</div><h1>寻龙诀任务台</h1></div><span class="version">v{html.escape(__version__)}</span></header>
<div class="grid"><section class="card"><h2>参数与启动指令</h2><p class="hint">这里只保存参数、拼接自然语言，不创建 Run、不领取任务。</p><div class="form-grid">
<label>站点<select id="site">{''.join(f'<option>{x}</option>' for x in ('US','UK','DE','FR','IT','ES','CA','JP'))}</select></label><label>根 Node ID<input id="root" inputmode="numeric" placeholder="例如 2975221011"></label><label class="wide">类目名<input id="category" placeholder="例如 Birds"></label><label>操作人<input id="operator" value="{html.escape(profile['display_name'], quote=True)}"></label><label>报告目录（可空）<input id="reportDir"></label>
<label>近6月均销门槛<input id="min_avg_sales" type="number"></label><label>最大层级<input id="max_level" type="number"></label><label>销量下降容忍<input id="sales_decline_tolerance" type="number" step=".01"></label><label>价格下降容忍<input id="price_decline_tolerance" type="number" step=".01"></label><label>最低 R²<input id="min_r_squared" type="number" step=".01"></label><label>批量<input id="batch_size" type="number"></label><label>重试次数<input id="max_attempts" type="number"></label><label>最大数据滞后月<input id="max_data_lag_months" type="number"></label></div>
<div class="actions"><button onclick="generate()">生成自然语言指令</button><button class="alt" onclick="copyInstruction()">复制指令</button></div><label class="wide" style="margin-top:12px">发送给 WorkBuddy<textarea id="instruction" readonly placeholder="填写参数后生成"></textarea></label>
<div class="presets"><h2>参数预设</h2><div class="preset-row"><input id="presetName" placeholder="预设名称，例如：美国站标准扫描"><button onclick="savePreset()">保存</button></div><div class="preset-row"><select id="presetSelect" onchange="loadPreset(this.value)"></select><button class="danger" onclick="deletePreset()">删除</button></div><div class="preset-meta">同名保存会更新预设；预设不包含运行状态。</div></div></section>
<section class="card"><div style="display:flex;justify-content:space-between;align-items:start"><div><h2>实时运行汇总</h2><p class="hint">每 3 秒读取 SQLite 状态；任何对话框都可查看。</p></div><button class="alt" onclick="refreshRuns()">立即刷新</button></div><div id="runs" class="run-list"><div class="empty">正在读取本机台账…</div></div></section></div></div><div class="toast" id="toast"></div>
<script>const BOOT={_script_json(boot)};const PKEYS=['min_avg_sales','max_level','sales_decline_tolerance','price_decline_tolerance','min_r_squared','batch_size','max_attempts','max_data_lag_months'];let presets=BOOT.presets;
function toast(t){{let e=document.getElementById('toast');e.textContent=t;e.style.display='block';setTimeout(()=>e.style.display='none',2200)}}
function payload(){{let p={{}};PKEYS.forEach(k=>p[k]=Number(document.getElementById(k).value));return{{site:site.value,root_node_id:root.value.trim(),category:category.value.trim(),operator_name:operator.value.trim(),report_dir:reportDir.value.trim(),policy:p}}}}
async function api(path,body){{let o={{headers:{{'X-XLJ-Panel-Token':BOOT.csrf}}}};if(body!==undefined){{o.method='POST';o.headers['Content-Type']='application/json';o.body=JSON.stringify(body)}}let r=await fetch(path,o),v=await r.json();if(!r.ok)throw Error(v.error||'请求失败');return v}}
function setPolicy(p){{PKEYS.forEach(k=>document.getElementById(k).value=p[k])}}setPolicy(BOOT.policy);
function renderPresets(){{presetSelect.innerHTML='<option value="">选择预设…</option>'+presets.map(p=>`<option value="${{p.preset_id}}">${{p.is_default?'★ ':''}}${{p.name}}</option>`).join('')}}renderPresets();
function loadPreset(id){{let p=presets.find(x=>x.preset_id===id);if(!p)return;site.value=p.site;root.value=p.root_node_id;category.value=p.category;reportDir.value=p.report_dir;setPolicy(p.policy);toast('已载入 '+p.name)}}
async function savePreset(){{try{{let v=payload();v.name=presetName.value.trim();if(!v.name)throw Error('请填写预设名称');let r=await api('/api/preset/save',v);presets=r.presets;renderPresets();presetSelect.value=r.preset.preset_id;toast('预设已保存')}}catch(e){{toast(e.message)}}}}
async function deletePreset(){{try{{if(!presetSelect.value)throw Error('请先选择预设');let r=await api('/api/preset/delete',{{preset_id:presetSelect.value}});presets=r.presets;renderPresets();toast('预设已删除')}}catch(e){{toast(e.message)}}}}
async function generate(){{try{{instruction.value=(await api('/api/instruction',payload())).instruction;toast('启动指令已生成')}}catch(e){{toast(e.message)}}}}
async function copyInstruction(){{if(!instruction.value)await generate();if(instruction.value){{await navigator.clipboard.writeText(instruction.value);toast('已复制，可粘贴到 WorkBuddy')}}}}
const labels={{FETCHING_TREND:'获取趋势',FETCHING_TREE:'获取子类目',CALLING_MCP:'调用 MCP',EVALUATING:'计算判定',WAITING_RETRY:'等待重试',INGESTING:'写入结果',ALL_DONE:'已完成',PARTIAL_DONE:'部分完成',RUNNING:'运行中'}};
function runCard(r){{let nc=r.node_counts||{{}},jc=r.job_counts||{{}},bp=r.batch_progress;let done=r.status==='ALL_DONE',warn=r.status==='PARTIAL_DONE';return `<article class="run"><div class="run-head"><div><div class="run-title">${{r.site}} · ${{r.category}}</div><div class="mono">${{r.run_id}} · 根节点 ${{r.root_node_id}}</div></div><span class="pill ${{done?'done':warn?'warn':''}}">${{labels[r.activity_status]||r.activity_status}}</span></div><div class="bar" title="${{r.progress_basis}}"><i style="width:${{r.completion_percent}}%"></i></div><div class="mono">当前已发现节点闭环率 ${{r.completion_percent}}% · L${{r.current_level}} · 最近更新 ${{r.updated_at}}</div><div class="metrics"><div class="metric"><b>${{r.total_nodes}}</b><small>发现节点</small></div><div class="metric"><b>${{r.terminal_nodes}}</b><small>已闭环</small></div><div class="metric"><b>${{jc.PENDING||0}}</b><small>待领取</small></div><div class="metric"><b>${{jc.LEASED||0}}</b><small>处理中</small></div><div class="metric"><b>${{r.opportunities}}</b><small>正式机会</small></div><div class="metric"><b>${{nc.ELIMINATED||0}}</b><small>趋势淘汰</small></div><div class="metric"><b>${{nc.POLICY_PRUNED||0}}</b><small>低体量剪枝</small></div><div class="metric"><b>${{nc.SHORT_HISTORY||0}}</b><small>短历史</small></div><div class="metric"><b>${{nc.SUSPENDED||0}}</b><small>暂停</small></div><div class="metric"><b>${{r.mcp_attempts}}</b><small>MCP尝试</small></div></div>${{bp?`<div class="batch">批次 ${{bp.batch_id.slice(0,8)}} · ${{labels[bp.stage]||bp.stage}} · ${{bp.processed}}/${{bp.total}}（${{bp.percent}}%） · 当前节点 ${{bp.current_node_id||'—'}} · 心跳 ${{bp.heartbeat_at||'等待首次上报'}}</div>`:''}}</article>`}}
async function refreshRuns(){{try{{let v=await api('/api/runs');runs.innerHTML=v.runs.length?v.runs.map(runCard).join(''):'<div class="empty">还没有 v4 运行记录。生成指令并在 WorkBuddy 中发送即可开始。</div>'}}catch(e){{runs.innerHTML='<div class="empty">读取失败：'+e.message+'</div>'}}}}refreshRuns();setInterval(refreshRuns,3000);
</script></body></html>'''.encode("utf-8")


def serve_panel(workspace: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, token: str = "") -> None:
    if host not in {"127.0.0.1", "localhost"}: raise ValueError("面板只允许绑定本机 127.0.0.1")
    workspace, token, started_at = os.path.abspath(workspace), token or secrets.token_urlsafe(24), utc_now()
    server_ref = {}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: bytes, code: int = 200, kind: str = "text/html; charset=utf-8"):
            self.send_response(code); self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def _json(self, value: object, code: int = 200):
            self._send(json.dumps(value, ensure_ascii=False).encode("utf-8"), code, "application/json; charset=utf-8")
        def _authorized(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-XLJ-Panel-Token", ""), token)
        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            value = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if not isinstance(value, dict): raise ValueError("请求体必须是对象")
            return value
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._json({"ok": True, "app": PANEL_APP, "version": __version__, "pid": os.getpid(),
                            "workspace_id": _workspace_id(workspace), "started_at": started_at}); return
            if parsed.path == "/api/runs": self._json({"runs": list_runs(workspace), "refreshed_at": utc_now()}); return
            if parsed.path == "/api/run":
                run_id = parse_qs(parsed.query).get("run", [""])[0]; self._json(status(workspace, run_id)); return
            if parsed.path == "/api/presets": self._json({"presets": list_presets(workspace)}); return
            if parsed.path == "/": self._send(_page(workspace, token)); return
            self._json({"error": "not found"}, 404)
        def do_POST(self):
            if self.path == "/shutdown":
                if not self._authorized(): self._json({"ok": False, "error": "forbidden"}, 403); return
                self._json({"ok": True, "stopping": True}); threading.Thread(target=server_ref["server"].shutdown, daemon=True).start(); return
            if not self._authorized(): self._json({"error": "安全令牌无效，请刷新页面"}, 403); return
            try:
                value = self._body()
                if self.path == "/api/instruction":
                    result = launch_instruction(value.get("site"), value.get("root_node_id"), value.get("category"),
                                                value.get("policy") or {}, value.get("operator_name", ""), value.get("report_dir", ""))
                    self._json({"instruction": result}); return
                if self.path == "/api/preset/save":
                    preset = save_preset(workspace, value.get("name"), value.get("site"), value.get("root_node_id", ""),
                                         value.get("category", ""), value.get("policy") or {}, value.get("report_dir", ""),
                                         value.get("description", ""), bool(value.get("is_default")))
                    self._json({"preset": preset, "presets": list_presets(workspace)}); return
                if self.path == "/api/preset/delete":
                    deleted = delete_preset(workspace, str(value.get("preset_id") or ""))
                    self._json({"deleted": deleted, "presets": list_presets(workspace)}); return
                if self.path == "/api/profile":
                    self._json(save_profile(workspace, value.get("display_name", ""), value.get("device_name", ""))); return
                raise ValueError("未知操作")
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
        def log_message(self, *_): pass

    server = ThreadingHTTPServer((DEFAULT_HOST, int(port)), Handler); server_ref["server"] = server
    _write_state(workspace, {"app": PANEL_APP, "version": __version__, "pid": os.getpid(), "host": DEFAULT_HOST,
                  "port": int(port), "url": f"http://{DEFAULT_HOST}:{int(port)}/", "token": token,
                  "workspace_id": _workspace_id(workspace), "started_at": started_at})
    try: server.serve_forever()
    finally:
        server.server_close(); current = _load_state(workspace)
        if current.get("token") == token:
            try: os.remove(_state_path(workspace))
            except FileNotFoundError: pass
