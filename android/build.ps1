# Build the Ten Forward phone app and put the APK where the server serves it (/app/TenForward.apk).
# Run:  powershell -ExecutionPolicy Bypass -File android\build.ps1  [-Debug]
param([switch]$Debug, [string]$Notes = "")

$ErrorActionPreference = "Stop"
$env:JAVA_HOME = "C:\Program Files\Eclipse Adoptium\jdk-17.0.18.8-hotspot"
$env:ANDROID_HOME = "C:\AndroidSDK"
$env:ANDROID_SDK_ROOT = "C:\AndroidSDK"
$env:Path = "$env:JAVA_HOME\bin;$env:Path"
Set-Location $PSScriptRoot

if (-not (Test-Path "local.properties")) { "sdk.dir=C\:\\AndroidSDK" | Out-File -Encoding ascii "local.properties" }

$task = if ($Debug) { ":app:assembleDebug" } else { ":app:assembleRelease" }
Write-Host "gradle $task ..."
.\gradlew.bat --no-daemon $task
if ($LASTEXITCODE -ne 0) { Write-Host "BUILD FAILED"; exit 1 }

$apk = if ($Debug) { "app\build\outputs\apk\debug\app-debug.apk" } else { "app\build\outputs\apk\release\app-release.apk" }
if (-not (Test-Path $apk)) { Write-Host "no APK at $apk"; exit 1 }

New-Item -ItemType Directory -Force -Path "..\dist" | Out-Null
Copy-Item $apk "..\dist\TenForward.apk" -Force

$v = (Select-String -Path "app\build.gradle" -Pattern "versionName '([^']+)'").Matches[0].Groups[1].Value
$c = (Select-String -Path "app\build.gradle" -Pattern "versionCode (\d+)").Matches[0].Groups[1].Value
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
New-Item -ItemType Directory -Force -Path "builds" | Out-Null
Copy-Item $apk ("builds\TenForward-v{0}-{1}-{2}.apk" -f $v, $c, $stamp)

$size = [math]::Round((Get-Item "..\dist\TenForward.apk").Length / 1MB, 2)

# what the app asks for at /api/app/version when it opens, so a phone knows it is behind
$meta = [ordered]@{
    version = $v
    build   = [int]$c
    size_mb = $size
    built   = [math]::Round((Get-Date -UFormat %s), 0)
    notes   = $Notes
}
$meta | ConvertTo-Json | Set-Content -Encoding utf8 "..\dist\app.json"

Write-Host ("OK -> dist\TenForward.apk ({0} MB, v{1} build {2})" -f $size, $v, $c)
Write-Host "The server hands it out at /app/TenForward.apk, and says so at /api/app/version"
