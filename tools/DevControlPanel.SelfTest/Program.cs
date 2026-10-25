using System.Text.Json;
using GeWuDevControl;

static void Check(bool value, string name)
{
    if (!value) throw new Exception("FAILED: " + name);
    Console.WriteLine("PASS " + name);
}

static RunCenterStatus Fixture(string state, bool verified, string mode = "live")
{
    var value = new
    {
        schema = 1, profile = "DEV", state, reason = "fixture", ownership_verified = verified, mode,
        policy = mode == "candidate" ? "offline" : "live_validation", instance_id = "DEV",
        url = mode == "candidate" ? "http://127.0.0.1:8896" : "http://127.0.0.1:5174",
        api_url = "http://127.0.0.1:8896", build_id = "0.5.0-beta.15+fixture",
        commit = "a".PadLeft(40, 'a'), storage = "K:\\GeWuDev\\Data",
        candidate_report = mode == "candidate" ? "K:\\GeWuDev\\Reports\\fixture.json" : null,
    };
    return RunCenterClient.ParseStatus(JsonSerializer.Serialize(value), "DEV");
}

var stopped = Fixture("stopped", false);
Check(stopped.CanStart && !stopped.CanOpen && !stopped.IsVerified, "stopped allows start only");
var live = Fixture("verified_running", true);
Check(!live.CanStart && live.CanOpen && live.IsVerified, "verified live allows open/control");
var candidate = Fixture("verified_running", true, "candidate");
Check(candidate.CanOpen && candidate.Policy == "offline" && candidate.CandidateReport!.EndsWith("fixture.json"),
    "candidate keeps report and offline identity");
foreach (var state in new[] { "degraded", "identity_conflict" })
{
    var item = Fixture(state, false);
    Check(!item.CanStart && !item.CanOpen && !item.IsVerified, state + " disables controls");
}
var wrong = false;
try { RunCenterClient.ParseStatus(JsonSerializer.Serialize(live), "PROD"); }
catch (InvalidDataException) { wrong = true; }
Check(wrong, "wrong profile rejected");
var statusArgs = RunCenterClient.Arguments("runner.ps1", "DEV", "Status");
Check(statusArgs.Contains("-Json") && !statusArgs.Contains("-NoOpen"), "status is JSON and read only");
var startArgs = RunCenterClient.Arguments("runner.ps1", "DEV", "Start", "offline", "K:\\Reports\\fixture.json");
Check(startArgs.Contains("-NoOpen") && startArgs.Contains("-DevCandidateReport"), "candidate start delegates validation");
var refused = false;
try { RunCenterClient.Arguments("runner.ps1", "PROD", "Stop"); }
catch (ArgumentException) { refused = true; }
Check(refused, "PROD control rejected");
refused = false;
try { RunCenterClient.Arguments("runner.ps1", "DEV", "Start", "online", "candidate.json"); }
catch (ArgumentException) { refused = true; }
Check(refused, "candidate cannot start online");
Check(!RunResult.Redact("api_key=abcd password:secret").Contains("abcd"), "diagnostic secrets redacted");

var fixtureRoot = Path.Combine(Path.GetTempPath(), "gewu-panel-selftest-" + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(fixtureRoot);
try
{
    File.WriteAllText(Path.Combine(fixtureRoot, "run-center.ps1"),
        "param([string]$Profile,[string]$Action,[string]$ExternalEffectsMode,[switch]$NoOpen,[switch]$Json)\n" +
        "if ($Action -eq 'Start') { [Console]::Error.WriteLine('maintenance lock: try later'); exit 7 }\n" +
        "[Console]::WriteLine('{\"schema\":1,\"profile\":\"DEV\",\"state\":\"stopped\",\"reason\":\"no_record_or_listener\",\"ownership_verified\":false,\"mode\":\"live\",\"url\":\"http://127.0.0.1:5174\",\"api_url\":\"http://127.0.0.1:8896\"}')\n");
    var fakeClient = new RunCenterClient(fixtureRoot);
    var fakeStatus = await fakeClient.StatusAsync("DEV");
    Check(fakeStatus.CanStart, "fake stopped service status parsed");
    var locked = await fakeClient.InvokeAsync("DEV", "Start", "offline");
    Check(!locked.Success && locked.ExitCode == 7 && locked.SafeError.Contains("maintenance lock"),
        "maintenance lock and start failure surface without retry");
}
finally
{
    Directory.Delete(fixtureRoot, recursive: true);
}
