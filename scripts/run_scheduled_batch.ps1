param(
    [string]$DataDir = "data",
    [string]$SourcesFile = "data\sources.json",
    [string]$Keywords = "AI,Agent,LLM,ML,Machine Learning,Systems,Backend,Python,Platform,Infrastructure",
    [int]$MaxApiCalls = 100,
    [switch]$LiveLlm,
    [string]$Model = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$runId = "sched-" + (Get-Date -Format "yyyyMMdd-HHmmss")
$cliArgs = @(
    "-m", "job_workflow.cli",
    "--data-dir", $DataDir,
    "run-sources",
    "--run-id", $runId,
    "--sources-file", $SourcesFile,
    "--max-api-calls", $MaxApiCalls
)

if ($Keywords -ne "") {
    $cliArgs += @("--keywords", $Keywords)
}

if ($LiveLlm) {
    $cliArgs += "--live-llm"
    if ($Model -ne "") {
        $cliArgs += @("--model", $Model)
    }
}

Write-Host "Starting scheduled run '$runId' using '$SourcesFile'..."
python @cliArgs
$exitCode = $LASTEXITCODE

Write-Host "`nPending approvals count:"
$pendingJson = python -m job_workflow.cli --data-dir $DataDir pending
$pendingList = $pendingJson | ConvertFrom-Json
Write-Host "$($pendingList.Count) casefile(s) awaiting approval."

if ($exitCode -ne 0) {
    Write-Warning "Run '$runId' finished with warnings/budget halt (exit code $exitCode). Inspect data\run_manifests\$runId.json."
}
