param(
    [switch]$Live,
    [string]$Storage = 'K:\Bobbytagging\Data-Test',
    [string]$ArchiveRun = 'K:\Bobbytagging\Data-Test\keyword-research\e4f2c3948bd044e3912c5eaeee184077'
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
$priorStorage = $env:TAG_STORAGE
try {
    $python = Join-Path $repoRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) { $python = 'python' }

    & $python -m pytest -q tests\test_product_archive_phase1.py tests\test_project_product_archive_flow.py `
        tests\test_product_archive_insights.py tests\test_product_archive_sync.py `
        tests\test_keyword_research_run.py tests\test_keyword_research_transport.py `
        tests\test_amazon_voc_reviews_bridge.py
    if ($LASTEXITCODE -ne 0) { throw 'Product Archive Phase 1 backend/MCP offline checks failed.' }

    npm --prefix frontend test
    if ($LASTEXITCODE -ne 0) { throw 'Product Archive frontend regression checks failed.' }

    npm --prefix frontend run build
    if ($LASTEXITCODE -ne 0) { throw 'Product Archive frontend build failed.' }

    Write-Output 'Offline Product Archive Phase 1 checks passed.'
    if ($Live) {
        if (-not (Test-Path -LiteralPath $Storage -PathType Container)) { throw 'Selected storage directory does not exist.' }
        if (-not (Test-Path -LiteralPath $ArchiveRun -PathType Container)) { throw 'Selected swimwear archive directory does not exist.' }
        $env:TAG_STORAGE = (Resolve-Path -LiteralPath $Storage).Path
        & $python scripts\verify_sellersprite_product_archive_live.py --live --storage $env:TAG_STORAGE --archive-run (Resolve-Path -LiteralPath $ArchiveRun).Path
        if ($LASTEXITCODE -ne 0) { throw 'The one-time live SellerSprite probe did not complete successfully.' }
    }
}
finally {
    if ($null -eq $priorStorage) { Remove-Item Env:TAG_STORAGE -ErrorAction SilentlyContinue }
    else { $env:TAG_STORAGE = $priorStorage }
    Pop-Location
}
