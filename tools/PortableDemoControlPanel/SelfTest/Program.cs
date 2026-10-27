using PortableDemoControlPanel;

static void Check(bool condition, string name)
{
    if (!condition) throw new InvalidOperationException("FAIL: " + name);
    Console.WriteLine("PASS: " + name);
}

var data = Path.Combine(Path.GetTempPath(), "GeWuDemo", "Data");
var correct = new DemoHealth("ok", DemoSafety.ExpectedApplication, DemoSafety.ExpectedBuildId,
    DemoSafety.ExpectedCommit, data);
Check(DemoSafety.IsVerified(correct, data), "accepts exact demo build and normalized data path");
Check(!DemoSafety.IsVerified(correct with { BuildId = "0.5.0-beta.14+deadbeef" }, data), "rejects a different build");
Check(!DemoSafety.IsVerified(correct with { Commit = "deadbeef" }, data), "rejects a different commit");
Check(!DemoSafety.IsVerified(correct with { Storage = Path.Combine(Path.GetTempPath(), "GeWuDemo", "Other") }, data), "rejects a different data directory");
Check(DemoSafety.IsVerified(correct with { Storage = Path.Combine(data, ".") }, data), "normalizes a trailing path segment");
Check(!DemoSafety.IsVerified(correct with { Application = "other-app" }, data), "rejects a different application");
var runtime = new DemoRuntime("dev", DemoSafety.ExpectedInstanceId, "live_validation");
Check(DemoSafety.IsVerified(correct, runtime, data), "requires the configured real-call demo runtime identity");
Check(!DemoSafety.IsVerified(correct, runtime with { Policy = "offline" }, data), "rejects a mismatched runtime policy");
const string credentialRuntime = "{\"profile\":\"dev\",\"instance_id\":\"DEMO\",\"external_effects\":{\"policy\":\"live_validation\"},\"credentials\":{\"model\":true,\"sellersprite\":false,\"sorftime\":true}}";
Check(DemoSafety.TryParseRuntime(credentialRuntime, out var parsedCredentials) &&
      parsedCredentials.ModelConfigured == true && parsedCredentials.SellerSpriteConfigured == false &&
      parsedCredentials.SorftimeConfigured == true, "reads only the three safe credential status booleans");
const string missingCredentialsRuntime = "{\"profile\":\"dev\",\"instance_id\":\"DEMO\",\"external_effects\":{\"policy\":\"live_validation\"}}";
Check(DemoSafety.TryParseRuntime(missingCredentialsRuntime, out var missingCredentials) &&
      missingCredentials.ModelConfigured is null && missingCredentials.SellerSpriteConfigured is null &&
      missingCredentials.SorftimeConfigured is null, "accepts missing credential fields as unknown");
const string partialCredentialsRuntime = "{\"profile\":\"dev\",\"instance_id\":\"DEMO\",\"external_effects\":{\"policy\":\"live_validation\"},\"credentials\":{\"model\":true}}";
Check(DemoSafety.TryParseRuntime(partialCredentialsRuntime, out var partialCredentials) &&
      partialCredentials.ModelConfigured == true && partialCredentials.SellerSpriteConfigured is null &&
      partialCredentials.SorftimeConfigured is null, "keeps omitted credential booleans unknown");
Check(DemoSafety.Evaluate(true, correct, runtime, data) == DemoState.Verified, "enables controls only for verified endpoint identity");
Check(DemoSafety.Evaluate(true, correct with { BuildId = "0.5.0-beta.14+deadbeef" }, runtime, data) == DemoState.IdentityConflict,
    "treats a busy port serving a different build as an identity conflict");
Check(DemoSafety.Evaluate(true, null, null, data) == DemoState.Stopped, "treats a stale endpoint with no listener as stopped");
Check(DemoSafety.Evaluate(true, null, null, data, listenerResponded: true) == DemoState.IdentityConflict,
    "treats a busy endpoint without a valid health identity as a port conflict");
Check(DemoSafety.TryParseEndpoint("{\"url\":\"http://127.0.0.1:18765\"}", out var endpoint) && endpoint.Port == 18765,
    "accepts launcher loopback endpoint");
Check(DemoSafety.BuildApiUri(endpoint.Url, "/api/health").AbsolutePath == "/api/health", "builds API URL without a double slash");
Check(!DemoSafety.TryParseEndpoint("{\"url\":\"http://192.168.1.9:18765\"}", out _), "rejects non-loopback endpoint");
Check(!DemoSafety.TryParseEndpoint("{\"url\":\"http://127.0.0.1:8765\"}", out _), "rejects ports outside demo range");
Check(!DemoSafety.TryParseEndpoint("{\"url\":\"http://127.0.0.1:18765/other\"}", out _), "rejects endpoint paths");
Check(!DemoSafety.TryParseEndpoint("[]", out _), "rejects malformed endpoint objects safely");
Check(!DemoSafety.TryParseRuntime("{\"external_effects\":[]}", out _), "rejects malformed runtime identity safely");
Console.WriteLine("All portable demo safety checks passed.");
