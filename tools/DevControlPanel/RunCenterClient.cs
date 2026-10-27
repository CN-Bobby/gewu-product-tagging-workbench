using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;

namespace GeWuDevControl;

public sealed class RunCenterStatus
{
    [JsonPropertyName("schema")] public int Schema { get; set; }
    [JsonPropertyName("profile")] public string Profile { get; set; } = "";
    [JsonPropertyName("state")] public string State { get; set; } = "";
    [JsonPropertyName("reason")] public string Reason { get; set; } = "";
    [JsonPropertyName("ownership_verified")] public bool OwnershipVerified { get; set; }
    [JsonPropertyName("mode")] public string Mode { get; set; } = "";
    [JsonPropertyName("policy")] public string? Policy { get; set; }
    [JsonPropertyName("instance_id")] public string? InstanceId { get; set; }
    [JsonPropertyName("url")] public string Url { get; set; } = "";
    [JsonPropertyName("api_url")] public string ApiUrl { get; set; } = "";
    [JsonPropertyName("build_id")] public string? BuildId { get; set; }
    [JsonPropertyName("commit")] public string? Commit { get; set; }
    [JsonPropertyName("storage")] public string? Storage { get; set; }
    [JsonPropertyName("candidate_report")] public string? CandidateReport { get; set; }

    public bool IsVerified => Schema == 1 && State == "verified_running" && OwnershipVerified;
    public bool CanStart => Schema == 1 && State == "stopped" && !OwnershipVerified;
    public bool CanClearStale => Schema == 1 && State == "degraded" &&
        Reason == "record_without_listener" && !OwnershipVerified;
    public bool CanOpen => IsVerified && Uri.TryCreate(Url, UriKind.Absolute, out var uri)
        && uri.Scheme == Uri.UriSchemeHttp && uri.IsLoopback;
}

public sealed record RunResult(int ExitCode, string Output, string Error)
{
    public bool Success => ExitCode == 0;
    public string SafeError => Redact(Error.Length > 0 ? Error : Output);

    public static string Redact(string? value)
    {
        value ??= "";
        var text = Regex.Replace(value, "(?i)(api[_-]?key|secret|password|authorization|bearer|token)\\s*[:=]\\s*[^\\s,;]+", "$1=[隐藏]");
        return text.Length > 1200 ? text[..1200] + "…" : text;
    }
}

public sealed class RunCenterClient
{
    public string Root { get; }
    public string ScriptPath => Path.Combine(Root, "run-center.ps1");
    public string ConfigPath => Path.Combine(Root, "config.json");

    public RunCenterClient(string? root = null)
    {
        Root = root ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "GeWuWorkbench", "RunCenter");
    }

    public static RunCenterStatus ParseStatus(string json, string expectedProfile)
    {
        var status = JsonSerializer.Deserialize<RunCenterStatus>(json.Trim())
            ?? throw new InvalidDataException("Run Center 未返回状态。");
        if (status.Schema != 1 || status.Profile != expectedProfile ||
            status.State is not ("stopped" or "verified_running" or "degraded" or "identity_conflict"))
            throw new InvalidDataException("Run Center 状态格式或环境身份不匹配。");
        if (status.IsVerified && (!Uri.TryCreate(status.ApiUrl, UriKind.Absolute, out var api) ||
            !api.IsLoopback || api.Scheme != Uri.UriSchemeHttp))
            throw new InvalidDataException("Run Center 返回了非本机 API 地址。");
        return status;
    }

    public static IReadOnlyList<string> Arguments(string scriptPath, string profile, string action,
        string? policy = null, string? candidateReport = null)
    {
        if (profile is not ("DEV" or "PROD") || action is not ("Status" or "Start" or "Stop"))
            throw new ArgumentException("不支持的环境或操作。");
        if (profile == "PROD" && action != "Status")
            throw new ArgumentException("控制面板只允许读取 PROD 状态。");
        var args = new List<string> { "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
            scriptPath, "-Profile", profile, "-Action", action };
        if (action == "Status") args.Add("-Json");
        else if (action == "Start")
        {
            if (policy is not ("online" or "offline")) throw new ArgumentException("请选择联网或离线策略。");
            args.AddRange(["-ExternalEffectsMode", policy, "-NoOpen"]);
            if (candidateReport is not null)
            {
                if (policy != "offline" || string.IsNullOrWhiteSpace(candidateReport))
                    throw new ArgumentException("候选只能离线启动，且必须提供报告。");
                args.AddRange(["-DevCandidateReport", candidateReport]);
            }
        }
        return args;
    }

    public async Task<RunResult> InvokeAsync(string profile, string action, string? policy = null,
        string? candidateReport = null)
    {
        if (!File.Exists(ScriptPath)) return new RunResult(2, "", "未找到已安装的 Run Center 脚本。");
        var args = Arguments(ScriptPath, profile, action, policy, candidateReport);
        var start = new ProcessStartInfo("powershell.exe")
        {
            UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true,
            RedirectStandardError = true, WorkingDirectory = Root,
        };
        // Desktop hosts can prepend a PowerShell 7 module directory that shadows
        // Windows PowerShell's Get-FileHash. Run Center needs only OS modules.
        start.Environment["PSModulePath"] = Path.Combine(Environment.SystemDirectory,
            "WindowsPowerShell", "v1.0", "Modules");
        foreach (var arg in args) start.ArgumentList.Add(arg);
        try
        {
            using var process = new Process { StartInfo = start };
            process.Start();
            var stdout = process.StandardOutput.ReadToEndAsync();
            var stderr = process.StandardError.ReadToEndAsync();
            using var timeout = new CancellationTokenSource(action == "Status"
                ? TimeSpan.FromSeconds(25) : TimeSpan.FromMinutes(3));
            try { await process.WaitForExitAsync(timeout.Token); }
            catch (OperationCanceledException)
            {
                // Never kill the process tree: Run Center may be in the middle of a safe start.
                return new RunResult(124, "", "运行中心尚未返回；请刷新状态核对，不要重复启动。");
            }
            return new RunResult(process.ExitCode, await stdout, await stderr);
        }
        catch (Exception ex) { return new RunResult(3, "", ex.Message); }
    }

    public async Task<RunCenterStatus> StatusAsync(string profile)
    {
        var result = await InvokeAsync(profile, "Status");
        if (!result.Success) throw new InvalidOperationException(result.SafeError);
        return ParseStatus(result.Output, profile);
    }

    public string ReportsDirectory()
    {
        try
        {
            using var document = JsonDocument.Parse(File.ReadAllText(ConfigPath));
            return document.RootElement.GetProperty("profiles").GetProperty("DEV").GetProperty("reports").GetString() ?? Root;
        }
        catch { return Root; }
    }
}
