using System.Diagnostics;
using System.Net;
using System.Net.Http.Json;
using System.Text.Json;

namespace PortableDemoControlPanel;

internal sealed class MainForm : Form
{
    private readonly string _root = Path.TrimEndingDirectorySeparator(AppContext.BaseDirectory);
    private readonly string _app;
    private readonly string _data;
    private readonly string _cache;
    private readonly string _workbench;
    private readonly string _endpointFile;
    private readonly string _logFile;
    private readonly string _relocationScript;
    private readonly string _python;
    private readonly Label _state = new();
    private readonly Label _details = new();
    private readonly Label _message = new();
    private readonly Button _start = MakeButton("启动演示环境");
    private readonly Button _stop = MakeButton("停止");
    private readonly Button _restart = MakeButton("重启");
    private readonly Button _open = MakeButton("打开工作台");
    private readonly Button _logs = MakeButton("查看日志");
    private readonly Button _refresh = MakeButton("刷新状态");
    private readonly NotifyIcon _tray;
    private readonly System.Windows.Forms.Timer _timer;
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(2) };
    private readonly bool _layoutValid;
    private string _layoutError = "";
    private DemoState _current = DemoState.NotStarted;
    private DemoEndpoint? _endpoint;
    private DemoRuntime? _runtime;
    private bool _busy;
    private bool _exit;

    public MainForm()
    {
        _app = Path.Combine(_root, "app");
        _data = Path.Combine(_root, "Data");
        _cache = Path.Combine(_root, "Cache");
        _workbench = Path.Combine(_app, "Workbench.exe");
        _endpointFile = Path.Combine(_data, "desktop-endpoint.json");
        _logFile = Path.Combine(_data, "logs", "desktop.log");
        _relocationScript = Path.Combine(_root, "relocate-demo-data.py");
        _python = Path.Combine(_app, "runtime", "python.exe");
        _layoutValid = ValidateLayout(out _layoutError);
        _start.Enabled = _stop.Enabled = _restart.Enabled = _open.Enabled = _logs.Enabled = false;

        Text = "格物 · 演示控制台";
        Font = new Font("Microsoft YaHei UI", 10.5f);
        BackColor = Color.FromArgb(244, 247, 243);
        ForeColor = Color.FromArgb(29, 55, 46);
        Size = new Size(900, 690);
        MinimumSize = new Size(650, 540);
        StartPosition = FormStartPosition.CenterScreen;
        AutoScaleMode = AutoScaleMode.Dpi;
        Icon = SystemIcons.Application;

        var body = new TableLayoutPanel
        {
            Dock = DockStyle.Fill, AutoScroll = true, ColumnCount = 1,
            Padding = new Padding(22), BackColor = BackColor,
        };
        body.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        Controls.Add(body);
        body.Controls.Add(Title("格物 · 演示控制台", 19, true));
        body.Controls.Add(Title("只管理本目录内的 beta.15 演示实例。关闭窗口后可从任务栏托盘重新打开。", 9.5f, false, Color.FromArgb(90, 111, 101)));

        var card = new TableLayoutPanel
        {
            Dock = DockStyle.Top, AutoSize = true, ColumnCount = 1,
            Padding = new Padding(20), Margin = new Padding(0, 18, 0, 8),
            BackColor = Color.White, CellBorderStyle = TableLayoutPanelCellBorderStyle.None,
        };
        card.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        card.Controls.Add(Title("演示实例", 14, true));
        _state.AutoSize = true;
        _state.Font = new Font("Microsoft YaHei UI", 15, FontStyle.Bold);
        _state.Margin = new Padding(0, 10, 0, 6);
        card.Controls.Add(_state);
        _details.AutoSize = true;
        _details.MaximumSize = new Size(790, 0);
        _details.ForeColor = Color.FromArgb(90, 111, 101);
        _details.Margin = new Padding(0, 0, 0, 14);
        card.Controls.Add(_details);

        var actions = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, WrapContents = true, Margin = Padding.Empty };
        _start.Click += async (_, _) => await StartAsync();
        _stop.Click += async (_, _) => await StopAsync();
        _restart.Click += async (_, _) => await RestartAsync();
        _open.Click += (_, _) => OpenWorkBench();
        _logs.Click += (_, _) => OpenLogs();
        _refresh.Click += async (_, _) => await RefreshAsync();
        actions.Controls.AddRange([_start, _stop, _restart, _open, _logs, _refresh]);
        card.Controls.Add(actions);
        _message.AutoSize = true;
        _message.MaximumSize = new Size(790, 0);
        _message.ForeColor = Color.FromArgb(149, 84, 31);
        _message.Margin = new Padding(0, 10, 0, 0);
        card.Controls.Add(_message);
        body.Controls.Add(card);

        var note = Title("API/MCP 调用由工作台自身设置和用户操作决定，控制台启动、停止、刷新本身不提交业务任务。", 9.5f, false, Color.FromArgb(90, 111, 101));
        note.Margin = new Padding(2, 6, 0, 0);
        body.Controls.Add(note);

        var menu = new ContextMenuStrip();
        menu.Items.Add("打开演示控制台", null, (_, _) => Restore());
        menu.Items.Add("退出控制台", null, (_, _) => ExitPanel());
        _tray = new NotifyIcon { Icon = SystemIcons.Application, Text = "格物演示控制台", ContextMenuStrip = menu, Visible = true };
        _tray.DoubleClick += (_, _) => Restore();
        _timer = new System.Windows.Forms.Timer { Interval = 5000 };
        _timer.Tick += async (_, _) => await RefreshAsync();
        _timer.Start();
        FormClosing += (_, e) =>
        {
            if (!_exit && e.CloseReason != CloseReason.WindowsShutDown) { e.Cancel = true; Hide(); }
        };
        FormClosed += (_, _) => { _timer.Dispose(); _http.Dispose(); _tray.Visible = false; _tray.Dispose(); menu.Dispose(); };
        Shown += async (_, _) => await RefreshAsync();
    }

    private static Label Title(string text, float size, bool bold, Color? color = null) => new()
    {
        Text = text, AutoSize = true, Font = new Font("Microsoft YaHei UI", size, bold ? FontStyle.Bold : FontStyle.Regular),
        ForeColor = color ?? Color.FromArgb(29, 55, 46), Margin = new Padding(0, 0, 0, 8),
    };

    private static Button MakeButton(string text) => new()
    {
        Text = text, AutoSize = true, MinimumSize = new Size(104, 42), Padding = new Padding(9, 4, 9, 4),
        FlatStyle = FlatStyle.Flat, BackColor = Color.White, ForeColor = Color.FromArgb(29, 55, 46),
        Margin = new Padding(0, 0, 9, 8), UseVisualStyleBackColor = false,
    };

    private bool ValidateLayout(out string error)
    {
        error = "";
        try
        {
            if ((File.GetAttributes(_root) & FileAttributes.ReparsePoint) != 0)
            { error = "控制台所在目录是链接目录；为避免写入其他位置，控制台已停用。"; return false; }
            if (!File.Exists(_workbench)) { error = "未找到 app\\Workbench.exe。请将控制台放在 app、Data、Cache 同级目录。"; return false; }
            var manifestPath = Path.Combine(_app, "build-manifest.json");
            if (!File.Exists(manifestPath)) { error = "未找到 app\\build-manifest.json，无法核实候选身份。"; return false; }
            using var manifest = JsonDocument.Parse(File.ReadAllText(manifestPath));
            var root = manifest.RootElement;
            if (root.GetProperty("build_id").GetString() != DemoSafety.ExpectedBuildId ||
                !string.Equals(root.GetProperty("commit").GetString(), DemoSafety.ExpectedCommit, StringComparison.OrdinalIgnoreCase))
            { error = "app 中的版本或提交与本控制台绑定的 beta.15 构建不一致。"; return false; }
            foreach (var dir in new[] { _app, _data, _cache })
            {
                if (Directory.Exists(dir) && (File.GetAttributes(dir) & FileAttributes.ReparsePoint) != 0)
                { error = "程序、Data 或 Cache 目录是链接目录；为避免写入其他位置，控制台已停用。"; return false; }
            }
            var rootFull = DemoSafety.NormalizePath(_root) + Path.DirectorySeparatorChar;
            if (!DemoSafety.NormalizePath(_app).StartsWith(rootFull, StringComparison.OrdinalIgnoreCase) ||
                !DemoSafety.NormalizePath(_data).StartsWith(rootFull, StringComparison.OrdinalIgnoreCase) ||
                !DemoSafety.NormalizePath(_cache).StartsWith(rootFull, StringComparison.OrdinalIgnoreCase))
            { error = "Data/Cache 路径不在控制台所在目录中。"; return false; }
            return true;
        }
        catch (Exception ex) { error = "无法核对演示目录：" + ex.Message; return false; }
    }

    private async Task RefreshAsync()
    {
        if (_busy) return;
        _endpoint = null;
        _runtime = null;
        DemoHealth? health = null;
        DemoRuntime? runtime = null;
        var endpointExists = File.Exists(_endpointFile);
        var state = DemoState.NotStarted;
        var note = "尚未由此控制台启动演示实例。";
        if (!_layoutValid)
        {
            note = _layoutError;
        }
        else if (endpointExists)
        {
            var endpointText = "";
            try { endpointText = await File.ReadAllTextAsync(_endpointFile); }
            catch (Exception ex) { note = "无法读取启动器 endpoint 文件：" + ex.Message; state = DemoState.IdentityConflict; }
            if (state != DemoState.IdentityConflict)
            {
                if (!DemoSafety.TryParseEndpoint(endpointText, out var endpoint))
                {
                    state = DemoState.IdentityConflict;
                    note = "endpoint 文件不是本机演示端口；为避免控制未知服务，操作已禁用。";
                }
                else
                {
                    _endpoint = endpoint;
                    var healthResult = await GetJsonAsync(DemoSafety.BuildApiUri(endpoint.Url, "/api/health"));
                    if (healthResult.ListenerResponded)
                    {
                        health = healthResult.Health;
                        var runtimeResult = await GetJsonAsync(DemoSafety.BuildApiUri(endpoint.Url, "/api/runtime/status"));
                        runtime = runtimeResult.Runtime;
                        _runtime = runtime;
                        state = DemoSafety.Evaluate(true, health, runtime, _data,
                            listenerResponded: healthResult.ListenerResponded || runtimeResult.ListenerResponded);
                        note = state == DemoState.Verified
                            ? "beta.15 身份已核实；页面与停止操作只针对此 loopback endpoint。"
                            : "端口有响应，但应用、构建、数据目录或运行策略身份不匹配；不会附加、停止或打开此服务。";
                    }
                    else
                    {
                        state = DemoState.Stopped;
                        note = "endpoint 文件存在，但端口当前没有服务响应。";
                    }
                }
            }
        }
        _current = state;
        _state.Text = state switch
        {
            DemoState.Verified => "运行中 · 身份已核实",
            DemoState.Stopped => "已停止",
            DemoState.IdentityConflict => "身份冲突 · 操作已禁用",
            _ => _layoutValid ? "未启动" : "目录未就绪",
        };
        _state.ForeColor = state == DemoState.Verified ? Color.FromArgb(34, 92, 67) : Color.FromArgb(149, 84, 31);
        var url = state == DemoState.Verified ? _endpoint?.Url.ToString() : "未核实";
        _details.Text = $"版本：{health?.BuildId ?? DemoSafety.ExpectedBuildId}   ·   提交：{Short(health?.Commit ?? DemoSafety.ExpectedCommit)}\n" +
            $"实例：{DemoSafety.ExpectedInstanceId}   ·   策略：允许模型/MCP 等外部调用\n" +
            $"数据：{_data}\n缓存：{_cache}\n页面：{url}\n" +
            (state == DemoState.Verified ? CredentialSummary(_runtime) + "\n" : "") +
            $"状态：{note}";
        _start.Enabled = !_busy && _layoutValid && (state is DemoState.NotStarted or DemoState.Stopped);
        _stop.Enabled = !_busy && state == DemoState.Verified;
        _restart.Enabled = !_busy && state == DemoState.Verified;
        _open.Enabled = !_busy && state == DemoState.Verified;
        _logs.Enabled = !_busy && File.Exists(_logFile);
        _refresh.Enabled = !_busy;
    }

    private static string Short(string commit) => commit.Length > 12 ? commit[..12] : commit;

    private static string CredentialSummary(DemoRuntime? runtime)
    {
        if (runtime is null) return "凭据状态：未提供，请在工作台「总设置」检查。";
        var missing = runtime.ModelConfigured != true || runtime.SellerSpriteConfigured != true || runtime.SorftimeConfigured != true;
        var summary = $"凭据：模型 {CredentialState(runtime.ModelConfigured)} / SellerSprite {CredentialState(runtime.SellerSpriteConfigured)} / Sorftime {CredentialState(runtime.SorftimeConfigured)}";
        return missing ? summary + "；待配置或未提供项请在工作台「总设置」检查。" : summary;
    }

    private static string CredentialState(bool? configured) => configured switch
    {
        true => "已配置",
        false => "待配置",
        _ => "未提供",
    };

    private async Task<(bool ListenerResponded, DemoHealth? Health, DemoRuntime? Runtime)> GetJsonAsync(Uri url)
    {
        try
        {
            using var response = await _http.GetAsync(url);
            if (!response.IsSuccessStatusCode) return (true, null, null);
            using var document = await JsonDocument.ParseAsync(await response.Content.ReadAsStreamAsync());
            var root = document.RootElement;
            if (url.AbsolutePath.Equals("/api/health", StringComparison.Ordinal))
            {
                if (!TryString(root, "status", out var status) || !TryString(root, "application", out var app) ||
                    !TryString(root, "build_id", out var build) || !TryString(root, "commit", out var commit) ||
                    !TryString(root, "storage", out var storage)) return (true, null, null);
                return (true, new DemoHealth(status, app, build, commit, storage), null);
            }
            if (DemoSafety.TryParseRuntime(root.GetRawText(), out var runtime)) return (true, null, runtime);
            return (true, null, null);
        }
        catch (HttpRequestException) { return (false, null, null); }
        catch (TaskCanceledException) { return (false, null, null); }
        catch (JsonException) { return (true, null, null); }
        catch (InvalidOperationException) { return (true, null, null); }
        catch (IOException) { return (false, null, null); }
    }

    private static bool TryString(JsonElement root, string name, out string value)
    {
        value = "";
        if (!root.TryGetProperty(name, out var property) || property.ValueKind != JsonValueKind.String) return false;
        value = property.GetString() ?? "";
        return true;
    }

    private async Task StartAsync()
    {
        if (_current is not (DemoState.NotStarted or DemoState.Stopped) || !_layoutValid || _busy) return;
        _busy = true;
        _message.Text = "正在准备演示目录…";
        await RefreshAsync();
        SetBusy(true);
        try
        {
            Directory.CreateDirectory(_data);
            Directory.CreateDirectory(_cache);
            if (!await RunRelocationIfNeededAsync()) return;
            var result = await RunWorkbenchAsync(stop: false);
            _message.Text = result.Success ? "启动器已返回，正在核验实例身份…" : "启动失败：" + result.Message;
        }
        finally { SetBusy(false); await RefreshAsync(); }
    }

    private async Task<bool> RunRelocationIfNeededAsync()
    {
        if (!File.Exists(_relocationScript)) return true;
        if (!File.Exists(_python))
        {
            _message.Text = "发现数据迁移脚本，但便携 Python 缺失，已取消启动。";
            return false;
        }
        _message.Text = "首次启动前正在迁移演示数据中的旧路径…";
        var result = await RunProcessAsync(_python, [_relocationScript, "--data-dir", _data], _root, online: false,
            timeout: TimeSpan.FromMinutes(10));
        if (!result.Success)
        {
            _message.Text = "演示数据迁移失败；实例未启动。" + result.Message;
            return false;
        }
        return true;
    }

    private async Task StopAsync()
    {
        if (_current != DemoState.Verified || _endpoint is null || _busy) return;
        if (!await ConfirmAsync("确定停止已核实的演示实例吗？正在运行的任务可能被中断。")) return;
        await ExecuteWorkbenchActionAsync(stop: true);
    }

    private async Task RestartAsync()
    {
        if (_current != DemoState.Verified || _endpoint is null || _busy) return;
        if (!await ConfirmAsync("确定重启已核实的演示实例吗？正在运行的任务可能被中断。")) return;
        await ExecuteWorkbenchActionAsync(stop: true, restart: true);
    }

    private async Task<bool> ConfirmAsync(string message)
    {
        var answer = MessageBox.Show(this, message, "格物演示控制台", MessageBoxButtons.YesNo,
            MessageBoxIcon.Warning, MessageBoxDefaultButton.Button2);
        await Task.CompletedTask;
        return answer == DialogResult.Yes;
    }

    private async Task ExecuteWorkbenchActionAsync(bool stop, bool restart = false)
    {
        _busy = true;
        SetBusy(true);
        try
        {
            if (!await VerifyCurrentAsync())
            {
                _message.Text = "实例身份刚刚发生变化；操作已取消，请刷新状态。";
                return;
            }
            var stopped = await RunWorkbenchAsync(true);
            if (!stopped.Success) { _message.Text = "停止未完成：" + stopped.Message; return; }
            _message.Text = "已请求 beta.15 启动器停止演示实例。";
            if (restart)
            {
                var started = await RunWorkbenchAsync(false);
                _message.Text = started.Success ? "已请求重启；正在重新核验身份。" : "重启启动阶段失败：" + started.Message;
            }
        }
        finally { SetBusy(false); await RefreshAsync(); }
    }

    private async Task<bool> VerifyCurrentAsync()
    {
        if (_endpoint is null) return false;
        var healthResult = await GetJsonAsync(DemoSafety.BuildApiUri(_endpoint.Url, "/api/health"));
        var runtimeResult = await GetJsonAsync(DemoSafety.BuildApiUri(_endpoint.Url, "/api/runtime/status"));
        return healthResult.ListenerResponded && runtimeResult.ListenerResponded &&
            DemoSafety.IsVerified(healthResult.Health, runtimeResult.Runtime, _data);
    }

    private async Task<(bool Success, string Message)> RunWorkbenchAsync(bool stop)
    {
        var port = _endpoint?.Port ?? DemoSafety.PreferredPort;
        var args = new List<string> { "--data", _data, "--cache", _cache, "--port", port.ToString(), "--no-open" };
        if (stop) args.Add("--stop");
        return await RunProcessAsync(_workbench, args, _app, online: true,
            timeout: stop ? TimeSpan.FromSeconds(90) : TimeSpan.FromSeconds(150));
    }

    private static async Task<(bool Success, string Message)> RunProcessAsync(string executable, IEnumerable<string> args,
        string workingDirectory, bool online, TimeSpan timeout)
    {
        var start = new ProcessStartInfo(executable)
        {
            UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = workingDirectory,
        };
        foreach (var arg in args) start.ArgumentList.Add(arg);
        start.Environment["GEWU_PROFILE"] = "dev";
        start.Environment["GEWU_DEV_EXTERNAL_EFFECTS"] = online ? "online" : "offline";
        start.Environment["GEWU_INSTANCE_ID"] = DemoSafety.ExpectedInstanceId;
        try
        {
            using var process = new Process { StartInfo = start };
            process.Start();
            using var cts = new CancellationTokenSource(timeout);
            try { await process.WaitForExitAsync(cts.Token); }
            catch (OperationCanceledException)
            {
                return (false, "启动器仍在运行或没有响应；没有结束进程，请刷新状态后再决定下一步。");
            }
            return process.ExitCode == 0 ? (true, "") : (false, $"进程退出码 {process.ExitCode}。请查看演示日志或迁移脚本报告。");
        }
        catch (Exception ex) { return (false, Redact(ex.Message)); }
    }

    private static string Redact(string text) => text.Length > 700 ? text[..700] + "…" : text;

    private void OpenWorkBench()
    {
        if (_current != DemoState.Verified || _endpoint is null) return;
        try { Process.Start(new ProcessStartInfo(_endpoint.Url.ToString()) { UseShellExecute = true }); }
        catch (Exception ex) { _message.Text = "无法打开浏览器：" + Redact(ex.Message); }
    }

    private void OpenLogs()
    {
        if (!_layoutValid || !File.Exists(_logFile)) return;
        try { Process.Start(new ProcessStartInfo(_logFile) { UseShellExecute = true }); }
        catch (Exception ex) { _message.Text = "无法打开日志：" + Redact(ex.Message); }
    }

    private void SetBusy(bool busy)
    {
        _busy = busy;
        _start.Enabled = _stop.Enabled = _restart.Enabled = _open.Enabled = _logs.Enabled = _refresh.Enabled = false;
        UseWaitCursor = busy;
    }

    private void Restore() { Show(); WindowState = FormWindowState.Normal; Activate(); }

    private void ExitPanel()
    {
        if (MessageBox.Show(this, "退出控制台不会停止正在运行的工作台。", "退出控制台",
                MessageBoxButtons.OKCancel, MessageBoxIcon.Information) != DialogResult.OK) return;
        _exit = true;
        Close();
    }
}
