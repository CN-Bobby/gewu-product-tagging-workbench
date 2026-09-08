function Get-WorkbenchHealth {
    param([string]$BaseUrl, [int]$TimeoutSec = 5)
    # Windows PowerShell 5 may decode JSON without charset using an ANSI code page.
    $response = Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/api/health" -TimeoutSec $TimeoutSec
    $stream = $response.RawContentStream
    $stream.Position = 0
    $reader = New-Object System.IO.StreamReader($stream, [Text.Encoding]::UTF8)
    try { return ($reader.ReadToEnd() | ConvertFrom-Json) }
    finally { $reader.Dispose() }
}
