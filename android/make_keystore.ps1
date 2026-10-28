# Make the signing key for the Ten Forward app, once. The key and its passwords stay out of git;
# keep a copy somewhere safe: an APK signed with a different key will not install over this one.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:JAVA_HOME = "C:\Program Files\Eclipse Adoptium\jdk-17.0.18.8-hotspot"

if (Test-Path "tenforward.jks") { Write-Host "tenforward.jks already exists, keeping it"; exit 0 }

$pw = -join ((48..57) + (97..122) | Get-Random -Count 24 | ForEach-Object { [char]$_ })
& "$env:JAVA_HOME\bin\keytool.exe" -genkeypair -v -keystore tenforward.jks -alias tenforward `
    -keyalg RSA -keysize 2048 -validity 10000 -storepass $pw -keypass $pw `
    -dname "CN=Ten Forward, OU=Radio, O=Ten Forward, C=US"
if ($LASTEXITCODE -ne 0) { Write-Host "keytool failed"; exit 1 }

@"
storeFile=tenforward.jks
storePassword=$pw
keyAlias=tenforward
keyPassword=$pw
"@ | Out-File -Encoding ascii "keystore.properties"

Write-Host "made tenforward.jks and keystore.properties (both stay out of git)"
Write-Host "back them up: a new key means the app has to be uninstalled before it can be updated"
