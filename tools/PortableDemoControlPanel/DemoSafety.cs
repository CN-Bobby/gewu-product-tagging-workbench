using System.Net;
using System.Text.Json;

namespace PortableDemoControlPanel;

public sealed record DemoHealth(string Status, string Application, string BuildId, string Commit, string Storage);
public sealed record DemoRuntime(string Profile, string InstanceId, string Policy,
    bool? ModelConfigured = null, bool? SellerSpriteConfigured = null, bool? SorftimeConfigured = null);
public sealed record DemoEndpoint(Uri Url)
{
    public int Port => Url.Port;
}

public enum DemoState { NotStarted, Stopped, Verified, IdentityConflict }

public static class DemoSafety
{
    public const string ExpectedApplication = "product-tagging-workbench";
    public const string ExpectedBuildId = "0.5.0-beta.15+ddc2b996e650";
    public const string ExpectedCommit = "ddc2b996e650b96b7bbbe69b57085fa18d116d38";
    public const string ExpectedProfile = "dev";
    public const string ExpectedInstanceId = "DEMO";
    public const string ExpectedPolicy = "live_validation";
    public const int PreferredPort = 18765;
    public const int MaximumPort = 18815;

    public static string NormalizePath(string path) => Path.TrimEndingDirectorySeparator(Path.GetFullPath(path));

    public static bool SamePath(string? left, string? right)
    {
        if (string.IsNullOrWhiteSpace(left) || string.IsNullOrWhiteSpace(right)) return false;
        try { return string.Equals(NormalizePath(left), NormalizePath(right), StringComparison.OrdinalIgnoreCase); }
        catch { return false; }
    }

    public static bool IsVerified(DemoHealth? health, string expectedData)
    {
        return health is not null && health.Status == "ok" &&
            health.Application == ExpectedApplication &&
            health.BuildId == ExpectedBuildId &&
            string.Equals(health.Commit, ExpectedCommit, StringComparison.OrdinalIgnoreCase) &&
            SamePath(health.Storage, expectedData);
    }

    public static bool IsDemoRuntime(string? profile, string? instanceId, string? policy) =>
        string.Equals(profile, ExpectedProfile, StringComparison.OrdinalIgnoreCase) &&
        string.Equals(instanceId, ExpectedInstanceId, StringComparison.Ordinal) &&
        string.Equals(policy, ExpectedPolicy, StringComparison.OrdinalIgnoreCase);

    public static bool IsVerified(DemoHealth? health, DemoRuntime? runtime, string expectedData) =>
        IsVerified(health, expectedData) && runtime is not null &&
        IsDemoRuntime(runtime.Profile, runtime.InstanceId, runtime.Policy);

    public static DemoState Evaluate(bool endpointExists, DemoHealth? health, DemoRuntime? runtime, string expectedData,
        bool listenerResponded = false)
    {
        if (!endpointExists) return DemoState.NotStarted;
        if (!listenerResponded && health is null && runtime is null) return DemoState.Stopped;
        return IsVerified(health, runtime, expectedData) ? DemoState.Verified : DemoState.IdentityConflict;
    }

    public static bool TryParseEndpoint(string json, out DemoEndpoint endpoint)
    {
        endpoint = default!;
        try
        {
            using var document = JsonDocument.Parse(json);
            if (!document.RootElement.TryGetProperty("url", out var value) || value.ValueKind != JsonValueKind.String)
                return false;
            if (!Uri.TryCreate(value.GetString(), UriKind.Absolute, out var uri) ||
                uri.Scheme != Uri.UriSchemeHttp || !IPAddress.TryParse(uri.Host, out var address) ||
                !IPAddress.IsLoopback(address) || uri.UserInfo.Length != 0 ||
                uri.AbsolutePath is not ("/" or "") || uri.Query.Length != 0 || uri.Fragment.Length != 0 ||
                uri.Port < PreferredPort || uri.Port > MaximumPort)
                return false;
            endpoint = new DemoEndpoint(uri);
            return true;
        }
        catch (Exception ex) when (ex is JsonException or InvalidOperationException or UriFormatException) { return false; }
    }

    public static Uri BuildApiUri(Uri endpoint, string path)
    {
        var builder = new UriBuilder(endpoint)
        {
            Path = "/" + path.TrimStart('/'),
            Query = "",
            Fragment = "",
        };
        return builder.Uri;
    }

    public static bool TryParseRuntime(string json, out DemoRuntime runtime)
    {
        runtime = default!;
        try
        {
            using var document = JsonDocument.Parse(json);
            var root = document.RootElement;
            if (!root.TryGetProperty("profile", out var profile) || profile.ValueKind != JsonValueKind.String ||
                !root.TryGetProperty("instance_id", out var instance) || instance.ValueKind != JsonValueKind.String ||
                !root.TryGetProperty("external_effects", out var effects) ||
                !effects.TryGetProperty("policy", out var policy) || policy.ValueKind != JsonValueKind.String)
                return false;
            var credentials = root.TryGetProperty("credentials", out var value) && value.ValueKind == JsonValueKind.Object
                ? value : default;
            runtime = new DemoRuntime(profile.GetString() ?? "", instance.GetString() ?? "", policy.GetString() ?? "",
                ReadBoolean(credentials, "model"), ReadBoolean(credentials, "sellersprite"), ReadBoolean(credentials, "sorftime"));
            return true;
        }
        catch (Exception ex) when (ex is JsonException or InvalidOperationException) { return false; }
    }

    private static bool? ReadBoolean(JsonElement element, string propertyName)
    {
        if (element.ValueKind != JsonValueKind.Object || !element.TryGetProperty(propertyName, out var value) ||
            value.ValueKind is not (JsonValueKind.True or JsonValueKind.False)) return null;
        return value.GetBoolean();
    }
}
