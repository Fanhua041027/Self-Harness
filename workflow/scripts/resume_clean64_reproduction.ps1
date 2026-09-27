param(
    [switch]$Foreground
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$runs = Join-Path $root "runs"
$baselineScript = Join-Path $PSScriptRoot "run_full_clean64_baselines.ps1"
$postScript = Join-Path $PSScriptRoot "run_clean64_post_baseline.ps1"

foreach ($name in @("SELF_HARNESS_DEEPSEEK_API_KEY", "SELF_HARNESS_QWEN_API_KEY")) {
    $value = [Environment]::GetEnvironmentVariable($name, "User")
    if (-not $value) { throw "$name is missing from the Windows user environment" }
}

docker info --format "{{.ServerVersion}}" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Docker is not available" }

$escapedRoot = [Regex]::Escape($root)
$active = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.CommandLine -match $escapedRoot -and
            $_.CommandLine -match "run_full_clean64_baselines|run_clean64_post_baseline|run_harbor_eval.py|run_self_harness_loop.py"
        }
)
if ($active.Count -gt 0) {
    Write-Output "Clean64 reproduction is already running (PID(s): $($active.ProcessId -join ', '))"
    exit 0
}

$command = @"
`$ErrorActionPreference = "Stop"
`$env:SELF_HARNESS_DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable("SELF_HARNESS_DEEPSEEK_API_KEY", "User")
`$env:SELF_HARNESS_QWEN_API_KEY = [Environment]::GetEnvironmentVariable("SELF_HARNESS_QWEN_API_KEY", "User")
`$env:PYTHONUTF8 = "1"
Set-Location -LiteralPath "$root"
& "$baselineScript"
& "$postScript"
"@

if ($Foreground) {
    Invoke-Expression $command
    exit $LASTEXITCODE
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$stdout = Join-Path $runs "clean64-resume-$stamp.stdout.log"
$stderr = Join-Path $runs "clean64-resume-$stamp.stderr.log"
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
$process = Start-Process -FilePath "powershell.exe" `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $encoded) `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -PassThru

Write-Output "Clean64 reproduction resumed in background (PID: $($process.Id))"
Write-Output "stdout: $stdout"
Write-Output "stderr: $stderr"
