# Mise à jour de katika237 avec le parser BeautifulSoup dès que la phase 01-katika du
# run 24 h est terminée : reparsage des pages en cache (--rebuild), poursuite de la
# collecte complète, cartographie du site (katika-map), puis extract + build + audit.
# Usage : powershell -ExecutionPolicy Bypass -File scripts\katika_update.ps1
$ErrorActionPreference = "Continue"
$exe = "C:\Users\BKNV2795\Application Data\Python\Python311\Scripts\cmrmp.exe"
if (-not (Test-Path $exe)) { $exe = "cmrmp" }
$root = Join-Path $PSScriptRoot ".."
$outDir = Join-Path $root "data\reports\day_run"
$dayLog = Join-Path $outDir "day_run.log"
$log = Join-Path $outDir "katika_update.log"
$deadline = (Get-Date).AddHours(8)

function Log($msg) {
    Add-Content -Path $log -Value ("{0} {1}" -f (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ"), $msg)
}

function Run-Step($name, $argList, $minutes) {
    $l = Join-Path $outDir "$name.log"
    $e = Join-Path $outDir "$name.err"
    Log "$name démarre (budget $minutes min)"
    $p = Start-Process -FilePath $exe -ArgumentList $argList -RedirectStandardOutput $l -RedirectStandardError $e -PassThru -WindowStyle Hidden
    if (-not $p.WaitForExit($minutes * 60 * 1000)) {
        try { $p.Kill() } catch {}
        Log "$name ARRÊTÉ après $minutes min"
    } else {
        Log "$name fini (code $($p.ExitCode))"
    }
}

# 1) Attendre la fin de la phase 01-katika du run 24 h (sinon deux crawlers sur le même hôte).
Log "attente de la fin de 01-katika..."
while ($true) {
    if ((Get-Date) -gt $deadline) { Log "délai dépassé sans fin de 01-katika : abandon"; exit 1 }
    $done = $false
    if (Test-Path $dayLog) {
        $done = (Get-Content $dayLog | Where-Object { $_ -match "01-katika" -and $_ -notmatch "d.{1,4}marre" }).Count -gt 0
    }
    if ($done) { Log "01-katika terminé : mise à jour katika démarre"; break }
    Start-Sleep -Seconds 120
}

# 2) Reparsage BeautifulSoup des pages en cache + poursuite de la collecte complète.
Run-Step "20-katika-update" @("crawl", "katika", "--years", "2018-2026", "--rebuild") 300

# 3) Cartographie complète du site (filtres, volumes par type et par année).
Run-Step "21-katika-map" @("crawl", "katika-map") 40

# 4) Raffraîchir tables et audit avec les données katika mises à jour.
Run-Step "22-extract" @("extract") 60
Run-Step "23-build" @("build") 40
Run-Step "24-audit" @("audit") 20
Log "mise à jour katika terminée"
