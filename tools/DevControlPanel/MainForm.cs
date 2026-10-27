using System.Diagnostics;
using System.Drawing;
using System.Text;

namespace GeWuDevControl;

public sealed class MainForm : Form
{
    private readonly RunCenterClient _client;
    private readonly FlowLayoutPanel _content = new();
    private readonly Panel _devCard = new();
    private readonly Panel _prodCard = new();
    private readonly Label _devState = new();
    private readonly Label _devDetail = new();
    private readonly Label _prodState = new();
    private readonly Label _prodDetail = new();
    private readonly Label _message = new();
    private readonly ComboBox _mode = new();
    private readonly TableLayoutPanel _reportRow = new();
    private readonly TextBox _report = new();
    private readonly Button _start = Button("启动 DEV", 108);
    private readonly Button _stop = Button("停止", 88);
    private readonly Button _restart = Button("重启当前模式", 128);
    private readonly Button _openDev = Button("打开 DEV", 108);
    private readonly Button _openProd = Button("打开 PROD", 108);
    private readonly FlowLayoutPanel _devActions = ButtonRow();
    private readonly FlowLayoutPanel _prodActions = ButtonRow();
    private readonly NotifyIcon _tray;
    private readonly System.Windows.Forms.Timer _timer;
    private RunCenterStatus? _dev;
    private RunCenterStatus? _prod;
    private RunResult? _lastAction;
    private bool _busy;
    private bool _refreshing;
    private bool _exitRequested;
    private bool _resizing;

    private static readonly Color BackgroundColor = Color.FromArgb(244, 247, 243);
    private static readonly Color TextColor = Color.FromArgb(29, 55, 46);
    private static readonly Color MutedColor = Color.FromArgb(90, 111, 101);
    private static readonly Color GreenColor = Color.FromArgb(34, 92, 67);
    private static readonly Color WarnColor = Color.FromArgb(149, 84, 31);
    private static readonly Font BodyFont = new("Microsoft YaHei UI", 10.5f);

    public MainForm(RunCenterClient client)
    {
        _client = client;
        Text = "格物 · 开发控制台";
        Font = BodyFont;
        BackColor = BackgroundColor;
        ForeColor = TextColor;
        Size = new Size(930, 900);
        MinimumSize = new Size(620, 520);
        StartPosition = FormStartPosition.CenterScreen;
        AutoScaleMode = AutoScaleMode.Dpi;
        Icon = SystemIcons.Application;

        _content.Dock = DockStyle.Fill;
        _content.AutoScroll = true;
        _content.FlowDirection = FlowDirection.TopDown;
        _content.WrapContents = false;
        _content.Padding = new Padding(20, 16, 20, 18);
        _content.BackColor = BackgroundColor;
        Controls.Add(_content);

        _content.Controls.Add(Header());
        _content.Controls.Add(BuildDevCard());
        _content.Controls.Add(BuildProdCard());
        _content.Controls.Add(Footer());
        _content.Resize += (_, _) => ResizeCards();
        Shown += async (_, _) => { ResizeCards(); await RefreshAsync(); };

        var menu = new ContextMenuStrip();
        menu.Items.Add("显示控制台", null, (_, _) => RestoreFromTray());
        menu.Items.Add("退出控制台", null, (_, _) => ExitPanel());
        _tray = new NotifyIcon { Icon = SystemIcons.Application, Text = "格物开发控制台", ContextMenuStrip = menu, Visible = true };
        _tray.DoubleClick += (_, _) => RestoreFromTray();
        _timer = new System.Windows.Forms.Timer { Interval = 15000 };
        _timer.Tick += async (_, _) => await RefreshAsync();
        _timer.Start();
        FormClosing += (_, e) =>
        {
            if (!_exitRequested && e.CloseReason != CloseReason.WindowsShutDown)
            {
                e.Cancel = true;
                Hide();
            }
        };
        FormClosed += (_, _) => { _timer.Dispose(); _tray.Visible = false; _tray.Dispose(); menu.Dispose(); };
    }

    private static Button Button(string title, int width)
    {
        return new Button
        {
            Text = title, Width = width, Height = 40, FlatStyle = FlatStyle.Flat,
            BackColor = Color.White, ForeColor = TextColor, Margin = new Padding(0, 0, 9, 9),
            UseVisualStyleBackColor = false,
        };
    }

    private static Label Label(string text, float size = 10.5f, bool bold = false, Color? color = null)
    {
        return new Label
        {
            Text = text, AutoSize = true, ForeColor = color ?? TextColor,
            Font = new Font("Microsoft YaHei UI", size, bold ? FontStyle.Bold : FontStyle.Regular),
            Margin = new Padding(0, 0, 0, 8),
        };
    }

    private Control Header()
    {
        var layout = Stack();
        layout.Controls.Add(Label("格物 · 开发控制台", 18, true));
        layout.Controls.Add(Label("运行状态、启动控制和故障线索集中在这里。关闭窗口后仍可从托盘打开。", 10, false, MutedColor));
        return layout;
    }

    private Panel BuildDevCard()
    {
        var stack = Stack();
        stack.Controls.Add(Label("DEV  开发环境", 14, true));
        _devState.Font = new Font("Microsoft YaHei UI", 14, FontStyle.Bold);
        _devState.AutoSize = true;
        _devState.Margin = new Padding(0, 3, 0, 8);
        stack.Controls.Add(_devState);
        _devDetail.AutoSize = true;
        _devDetail.ForeColor = MutedColor;
        _devDetail.Margin = new Padding(0, 0, 0, 14);
        stack.Controls.Add(_devDetail);

        stack.Controls.Add(Label("启动方式", 10, true));
        _mode.DropDownStyle = ComboBoxStyle.DropDownList;
        _mode.Width = 280;
        _mode.Height = 36;
        _mode.Items.AddRange(["热更新 · 允许外部调用", "热更新 · 离线", "已冻结候选 · 离线"]);
        _mode.SelectedIndex = 0;
        _mode.SelectedIndexChanged += (_, _) => _reportRow.Visible = _mode.SelectedIndex == 2;
        stack.Controls.Add(_mode);
        stack.Controls.Add(Label("启动本身不发起模型、采集或 VOC 任务；联网模式下，页面任务可能产生费用。", 9, false, MutedColor));

        _reportRow.AutoSize = false;
        _reportRow.Height = 80;
        _reportRow.ColumnCount = 2;
        _reportRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        _reportRow.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 102));
        _reportRow.RowCount = 2;
        _reportRow.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        _reportRow.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        _reportRow.Controls.Add(Label("候选报告（由 Run Center 复核）", 9, true), 0, 0);
        _reportRow.SetColumnSpan(_reportRow.GetControlFromPosition(0, 0)!, 2);
        _report.Dock = DockStyle.Fill;
        _report.Margin = new Padding(0, 0, 8, 10);
        _report.AccessibleName = "候选报告路径";
        _reportRow.Controls.Add(_report, 0, 1);
        var browse = Button("选择报告", 92);
        browse.Click += (_, _) => BrowseReport();
        _reportRow.Controls.Add(browse, 1, 1);
        _reportRow.Visible = false;
        stack.Controls.Add(_reportRow);

        var actions = _devActions;
        _start.BackColor = GreenColor; _start.ForeColor = Color.White;
        _start.Click += async (_, _) => await StartAsync();
        _stop.Click += async (_, _) => await StopAsync();
        _restart.Click += async (_, _) => await RestartAsync();
        _openDev.Click += (_, _) => OpenPage(_dev);
        var logs = Button("查看日志", 99);
        logs.Click += (_, _) => OpenLogs();
        var copy = Button("复制诊断", 99);
        copy.Click += (_, _) => CopyDiagnostics();
        var refresh = Button("刷新状态", 99);
        refresh.Click += async (_, _) => await RefreshAsync();
        actions.Controls.AddRange([_start, _stop, _restart, _openDev, logs, copy, refresh]);
        stack.Controls.Add(actions);

        _message.AutoSize = true;
        _message.ForeColor = WarnColor;
        _message.Margin = new Padding(0, 4, 0, 0);
        stack.Controls.Add(_message);
        StyleCard(_devCard, stack);
        _devCard.Height = 430;
        return _devCard;
    }

    private Panel BuildProdCard()
    {
        var stack = Stack();
        stack.Controls.Add(Label("PROD  正式环境 · 只读", 13, true));
        _prodState.Font = new Font("Microsoft YaHei UI", 12, FontStyle.Bold);
        _prodState.AutoSize = true;
        _prodState.Margin = new Padding(0, 2, 0, 7);
        stack.Controls.Add(_prodState);
        _prodDetail.AutoSize = true;
        _prodDetail.ForeColor = MutedColor;
        _prodDetail.Margin = new Padding(0, 0, 0, 11);
        stack.Controls.Add(_prodDetail);
        var actions = _prodActions;
        _openProd.Click += (_, _) => OpenPage(_prod);
        actions.Controls.Add(_openProd);
        stack.Controls.Add(actions);
        StyleCard(_prodCard, stack);
        _prodCard.Height = 240;
        return _prodCard;
    }

    private Control Footer()
    {
        var label = Label("面板仅调用当前用户的 Run Center；遇到身份冲突时保留现场，不接管未知进程。", 9, false, MutedColor);
        label.Padding = new Padding(2, 7, 0, 0);
        return label;
    }

    private static TableLayoutPanel Stack()
    {
        var stack = new TableLayoutPanel { ColumnCount = 1, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink,
            Dock = DockStyle.Top, Margin = Padding.Empty };
        stack.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        return stack;
    }

    private static FlowLayoutPanel ButtonRow() => new()
    {
        AutoSize = false, FlowDirection = FlowDirection.LeftToRight,
        WrapContents = true, Margin = new Padding(0, 5, 0, 0),
    };

    private static void StyleCard(Panel card, TableLayoutPanel stack)
    {
        card.AutoSize = false;
        card.BackColor = Color.White;
        card.BorderStyle = BorderStyle.FixedSingle;
        card.Padding = new Padding(20, 18, 20, 15);
        card.Margin = new Padding(0, 0, 0, 14);
        stack.AutoSize = false;
        stack.Dock = DockStyle.Fill;
        card.Controls.Add(stack);
    }

    private void ResizeCards()
    {
        if (_resizing) return;
        _resizing = true;
        try
        {
        var width = Math.Max(510, _content.ClientSize.Width - _content.Padding.Horizontal - 24);
        foreach (Control card in _content.Controls) card.Width = width;
        var inner = width - _devCard.Padding.Horizontal - 2;
        _devCard.Controls[0].Width = inner;
        _prodCard.Controls[0].Width = width - _prodCard.Padding.Horizontal - 2;
        _reportRow.Width = inner;
        _devDetail.MaximumSize = new Size(inner - 5, 0);
        _prodDetail.MaximumSize = new Size(inner - 5, 0);
        _message.MaximumSize = new Size(inner - 5, 0);
        SizeButtonRow(_devActions, inner);
        SizeButtonRow(_prodActions, inner);
        _devCard.Height = Math.Max(430, _devCard.Controls[0].GetPreferredSize(new Size(inner, 0)).Height
            + _devCard.Padding.Vertical + 4);
        _prodCard.Height = Math.Max(240, _prodCard.Controls[0].GetPreferredSize(new Size(inner, 0)).Height
            + _prodCard.Padding.Vertical + 4);
        }
        finally { _resizing = false; }
    }

    private static void SizeButtonRow(FlowLayoutPanel row, int width)
    {
        row.Width = width;
        var lineWidth = 0;
        var rows = 1;
        foreach (Control button in row.Controls)
        {
            var itemWidth = button.Width + button.Margin.Horizontal;
            if (lineWidth > 0 && lineWidth + itemWidth > width) { rows++; lineWidth = 0; }
            lineWidth += itemWidth;
        }
        row.Height = rows * 49;
    }

    private async Task RefreshAsync()
    {
        if (_refreshing || _busy) return;
        _refreshing = true;
        try
        {
            var devTask = SafeStatus("DEV");
            var prodTask = SafeStatus("PROD");
            var results = await Task.WhenAll(devTask, prodTask);
            _dev = results[0].status;
            _prod = results[1].status;
            if (results[0].error is not null) _message.Text = "DEV 状态读取失败：" + RunResult.Redact(results[0].error);
            else if (results[1].error is not null) _message.Text = "PROD 状态读取失败：" + RunResult.Redact(results[1].error);
            Render();
        }
        finally { _refreshing = false; }
    }

    private async Task<(RunCenterStatus? status, string? error)> SafeStatus(string profile)
    {
        try { return (await _client.StatusAsync(profile), null); }
        catch (Exception ex) { return (null, ex.Message); }
    }

    private void Render()
    {
        _devState.Text = StateText(_dev);
        _devState.ForeColor = _dev?.IsVerified == true ? GreenColor : WarnColor;
        _devDetail.Text = DetailText(_dev);
        _prodState.Text = StateText(_prod);
        _prodState.ForeColor = _prod?.IsVerified == true ? GreenColor : WarnColor;
        _prodDetail.Text = DetailText(_prod);
        _start.Enabled = !_busy && _dev?.CanStart == true;
        _start.BackColor = _start.Enabled ? GreenColor : Color.FromArgb(226, 233, 226);
        _start.ForeColor = _start.Enabled ? Color.White : MutedColor;
        _stop.Enabled = !_busy && (_dev?.IsVerified == true || _dev?.CanClearStale == true);
        _stop.Text = _dev?.CanClearStale == true ? "清理失效记录" : "停止";
        _restart.Enabled = !_busy && _dev?.IsVerified == true;
        _openDev.Enabled = !_busy && _dev?.CanOpen == true;
        _openProd.Enabled = _prod?.CanOpen == true;
        if (_dev?.IsVerified == true)
        {
            if (_dev.Mode == "candidate")
            {
                _mode.SelectedIndex = 2;
                _report.Text = _dev.CandidateReport ?? "";
            }
            else _mode.SelectedIndex = _dev.Policy == "offline" ? 1 : 0;
        }
        ResizeCards();
    }

    private static string StateText(RunCenterStatus? status) => status?.State switch
    {
        "verified_running" => "运行中 · 身份已核实",
        "stopped" => "已停止",
        "degraded" => "运行异常 · 请查看诊断",
        "identity_conflict" => "身份冲突 · 已禁用控制",
        _ => "状态不可用",
    };

    private static string DetailText(RunCenterStatus? status)
    {
        if (status is null) return "无法读取 Run Center 状态；请检查安装文件。";
        var mode = status.Mode switch { "candidate" => "冻结候选", "live" => "源码热更新", _ => "正式程序" };
        var policy = status.Policy switch { "offline" => "离线", "live_validation" => "允许外部调用", "online" => "在线", _ => "未运行" };
        return $"{mode}  ·  {policy}  ·  {status.BuildId ?? "无运行版本"}\n" +
            $"页面：{status.Url}    数据：{status.Storage ?? "未知"}\n" +
            $"诊断：{status.Reason}";
    }

    private void BrowseReport()
    {
        using var dialog = new OpenFileDialog { Title = "选择 DEV 候选报告", Filter = "JSON 报告 (*.json)|*.json",
            InitialDirectory = _client.ReportsDirectory(), CheckFileExists = true };
        if (dialog.ShowDialog(this) == DialogResult.OK) _report.Text = dialog.FileName;
    }

    private async Task StartAsync()
    {
        if (_dev?.CanStart != true || _busy) return;
        var candidate = _mode.SelectedIndex == 2 ? _report.Text.Trim() : null;
        if (_mode.SelectedIndex == 2 && !File.Exists(candidate))
        {
            _message.Text = "请先选择存在的候选报告。";
            return;
        }
        await ActAsync("Start", _mode.SelectedIndex == 0 ? "online" : "offline", candidate);
    }

    private async Task StopAsync()
    {
        if (_busy || (_dev?.IsVerified != true && _dev?.CanClearStale != true)) return;
        if (_dev?.CanClearStale == true)
        {
            if (MessageBox.Show(this,
                "Run Center 留有上次的 DEV 记录，但服务端口已停止。清理前会再次核对记录与端口；身份不符时会保留现场。确定清理吗？",
                "清理失效记录", MessageBoxButtons.YesNo, MessageBoxIcon.Warning,
                MessageBoxDefaultButton.Button2) != DialogResult.Yes) return;
        }
        else if (!ConfirmInterruption("停止")) return;
        await ActAsync("Stop");
    }

    private async Task RestartAsync()
    {
        if (_dev?.IsVerified != true || _busy) return;
        if (!ConfirmInterruption("重启")) return;
        var policy = _dev.Mode == "candidate" || _dev.Policy == "offline" ? "offline" : "online";
        var candidate = _dev.Mode == "candidate" ? _dev.CandidateReport : null;
        _busy = true;
        Render();
        try
        {
            _message.Text = "正在停止已核实的 DEV…";
            var stopped = await _client.InvokeAsync("DEV", "Stop");
            _lastAction = stopped;
            if (!stopped.Success) { _message.Text = "停止失败：" + stopped.SafeError; return; }
            _message.Text = "正在按原模式启动 DEV…";
            var started = await _client.InvokeAsync("DEV", "Start", policy, candidate);
            _lastAction = started;
            _message.Text = started.Success ? "已按原模式重启，请核对状态。" : "启动失败：" + started.SafeError;
        }
        finally { _busy = false; await RefreshAsync(); }
    }

    private bool ConfirmInterruption(string action) => MessageBox.Show(this,
        $"确定{action} DEV 吗？正在运行的任务可能被中断。控制台只会操作 Run Center 已核实的 DEV 进程。",
        $"确认{action}", MessageBoxButtons.YesNo, MessageBoxIcon.Warning,
        MessageBoxDefaultButton.Button2) == DialogResult.Yes;

    private async Task ActAsync(string action, string? policy = null, string? candidate = null)
    {
        _busy = true;
        Render();
        try
        {
            _message.Text = action == "Start" ? "Run Center 正在启动 DEV…" : "Run Center 正在停止 DEV…";
            _lastAction = await _client.InvokeAsync("DEV", action, policy, candidate);
            _message.Text = _lastAction.Success ? "操作完成，正在核对实时身份。" : "操作失败：" + _lastAction.SafeError;
        }
        finally { _busy = false; await RefreshAsync(); }
    }

    private void OpenPage(RunCenterStatus? status)
    {
        if (status?.CanOpen != true) return;
        try { Process.Start(new ProcessStartInfo(status.Url) { UseShellExecute = true }); }
        catch (Exception ex) { _message.Text = "浏览器未能打开：" + RunResult.Redact(ex.Message); }
    }

    private void OpenLogs()
    {
        var path = Path.Combine(_client.Root, "logs", "DEV");
        if (!Directory.Exists(path)) { _message.Text = "目前还没有 DEV 日志目录。"; return; }
        try { Process.Start(new ProcessStartInfo("explorer.exe") { ArgumentList = { path }, UseShellExecute = true }); }
        catch (Exception ex) { _message.Text = "日志目录未能打开：" + RunResult.Redact(ex.Message); }
    }

    private void CopyDiagnostics()
    {
        var summary = new StringBuilder("格物开发控制台诊断\n");
        foreach (var item in new[] { _dev, _prod })
            if (item is not null)
                summary.AppendLine($"{item.Profile}: {item.State}/{item.Reason}; mode={item.Mode}; policy={item.Policy}; " +
                    $"build={item.BuildId}; commit={item.Commit}; url={item.Url}; storage={item.Storage}");
        if (_lastAction is not null)
            summary.AppendLine($"最近操作: exit={_lastAction.ExitCode}; {RunResult.Redact(_lastAction.SafeError)}");
        try { Clipboard.SetText(summary.ToString()); _message.Text = "已复制不含密钥的状态摘要。"; }
        catch (Exception ex) { _message.Text = "复制失败：" + RunResult.Redact(ex.Message); }
    }

    public void RestoreFromTray()
    {
        Show(); WindowState = FormWindowState.Normal; Activate();
    }

    private void ExitPanel()
    {
        _exitRequested = true;
        Close();
    }
}
