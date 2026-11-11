# Run de collecte bout en bout sur une journée (24 h), relançable.
# Usage : powershell -ExecutionPolicy Bypass -File scripts\day_run.ps1 [total_hours=24]
# Chaque phase a un budget de temps ; un processus dépassé est arrêté, la collecte
# reprendra au lancement suivant (cache disque + files JSONL dédupliquées).
param([int]$TotalHours = 24)

$ErrorActionPreference = "Continue"
$exe = "C:\Users\BKNV2795\Application Data\Python\Python311\Scripts\cmrmp.exe"
if (-not (Test-Path $exe)) { $exe = "cmrmp" }
$outDir = Join-Path $PSScriptRoot "..\data\reports\day_run"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$deadline = (Get-Date).AddHours($TotalHours)
$summary = [ordered]@{
    started_at_utc  = (Get-Date).ToUniversalTime().ToString("o")
    total_hours     = $TotalHours
    phases          = [ordered]@{}
}

function Log($msg) {
    $line = "{0} {1}" -f (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ"), $msg
    Add-Content -Path (Join-Path $outDir "day_run.log") -Value $line
}

function Run-Step($name, $argList, $minutes) {
    $log = Join-Path $outDir "$name.log"
    $err = Join-Path $outDir "$name.err"
    $left = ($deadline - (Get-Date)).TotalMinutes
    if ($left -le 1) { Log "$name sauté : délai global atteint"; $summary.phases[$name] = "skipped"; return }
    $mins = [int][Math]::Min($minutes, $left)
    Log "$name démarre (budget ${mins} min)"
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $p = Start-Process -FilePath $exe -ArgumentList $argList -RedirectStandardOutput $log -RedirectStandardError $err -PassThru -WindowStyle Hidden
    if (-not $p.WaitForExit($mins * 60 * 1000)) {
        try { $p.Kill() } catch {}
        Log "$name ARRÊTÉ après ${mins} min (délai de phase)"
        $summary.phases[$name] = "timeout_after_${mins}min"
    } else {
        $code = $p.ExitCode
        Log "$name fini en $([int]($sw.Elapsed.TotalMinutes)) min (code $code)"
        $summary.phases[$name] = "exit_$code"
    }
    $sw.Stop()
}

Log "=== run 24h démarre ==="

# Phase 1 : collecte par source (séquentielle, budget partage la fenêtre)
Run-Step "01-katika"    @("crawl", "katika", "--years", "2018-2026")               360
Run-Step "02-armp"      @("crawl", "armp")                                          120
Run-Step "03-pridesoft" @("crawl", "pridesoft", "--start", "6500", "--end", "20000") 240
Run-Step "04-minmap"    @("crawl", "minmap")                                         30
Run-Step "05-coleps"    @("crawl", "coleps")                                        120
Run-Step "06-sanctions" @("crawl", "armp-sanctions")                                15
Run-Step "07-experts"   @("crawl", "armp-experts")                                  15

# Phase 2 : documents mis en file (budget ~ 2 h, limité au temps restant)
Run-Step "08-documents" @("crawl", "documents")                                     120

# Phase 3 : extraction, construction, audit
Run-Step "09-extract"   @("extract")                                                 60
Run-Step "10-build"     @("build")                                                   30
Run-Step "11-audit"     @("audit")                                                   20

# Comptages finaux
$counts = [ordered]@{}
foreach ($pair in @(
    @("raw", "data\raw"), @("raw_docs", "data\raw\docs"),
    @("processed", "data\processed"), @("text", "data\text"))) {
    $dir = Join-Path $PSScriptRoot ".." | Join-Path -ChildPath $pair[1]
    if (Test-Path $dir) {
        $counts[$pair[0]] = (Get-ChildItem $dir -Recurse -File | Measure-Object).Count
    }
}
$counts["interim_lines"] = [ordered]@{}
Get-ChildItem (Join-Path $PSScriptRoot "..\data\interim") -Recurse -Filter *.jsonl -ErrorAction SilentlyContinue | ForEach-Object {
    $n = (Get-Content $_.FullName | Measure-Object -Line).Lines
    $counts["interim_lines"][$_.BaseName] = $n
}

$summary.finished_at_utc = (Get-Date).ToUniversalTime().ToString("o")
$summary.counts = $counts
$summary | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $outDir "summary.json") -Encoding UTF8
Log "=== run 24h terminé ==="
