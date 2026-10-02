$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    $python = Join-Path $repoRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) { $python = 'python' }
    & $python -m pytest -q tests\test_amazon_voc_workbench.py tests\test_amazon_voc_reviews_bridge.py
    if ($LASTEXITCODE -ne 0) { throw 'Amazon VOC Phase 2 backend tests failed.' }

    npm --prefix frontend run test:amazon-voc
    if ($LASTEXITCODE -ne 0) { throw 'Amazon VOC Phase 2 frontend tests failed.' }
    Write-Output 'Amazon VOC Phase 2 deterministic integration tests passed.'
}
finally {
    Pop-Location
}
