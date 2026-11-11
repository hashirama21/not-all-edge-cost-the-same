# Push temps reel vers le bucket Hugging Face pendant les runs longs.
# Usage : powershell -ExecutionPolicy Bypass -File scripts\hf_watch.ps1 [-Bucket user/cmr-marches-brut] [-IntervalS 1200]
# Lit HF_TOKEN dans .env (charge par cmrmp) ; logue dans data\reports\day_run\hf_watch.log.
param([string]$Bucket = "krohn/cmr-marches-brut", [int]$IntervalS = 1200)

$ErrorActionPreference = "Continue"
$exe = "C:\Users\BKNV2795\Application Data\Python\Python311\Scripts\cmrmp.exe"
if (-not (Test-Path $exe)) { $exe = "cmrmp" }
$outDir = Join-Path $PSScriptRoot "..\data\reports\day_run"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$log = Join-Path $outDir "hf_watch.log"

function Log($msg) {
    Add-Content -Path $log -Value ("{0} {1}" -f (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ"), $msg)
}

Log "watch HF demarre (bucket $Bucket, intervalle ${IntervalS}s)"
while ($true) {
    if (-not (Test-Path (Join-Path $PSScriptRoot "..\.env"))) {
        Log "en attente : .env absent (ajoutez HF_TOKEN=hf_xxx dans .env)"
        Start-Sleep -Seconds 60
        continue
    }
    $p = Start-Process -FilePath $exe -ArgumentList "hf", "watch", "--bucket", $Bucket, "--interval-s", "$IntervalS" `
        -RedirectStandardOutput (Join-Path $outDir "hf_push.out") -RedirectStandardError (Join-Path $outDir "hf_push.err") `
        -PassThru -WindowStyle Hidden
    Log "hf watch lance (pid $($p.Id))"
    $p.WaitForExit() | Out-Null
    Log "hf watch termine (code $($p.ExitCode)) - relance dans 60 s"
    Start-Sleep -Seconds 60
}
