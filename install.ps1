$ErrorActionPreference = "Stop"

# 1. Pre-flight check for yt-dlp
if (-not (Get-Command yt-dlp -ErrorAction SilentlyContinue)) {
    Write-Host "yt-dlp was not found in your PATH." -ForegroundColor Yellow
    Write-Host "Install yt-dlp (along with FFmpeg and Deno) first by running:" -ForegroundColor Cyan
    Write-Host "  winget install yt-dlp.yt-dlp" -ForegroundColor Green
    Write-Host "`nAfter installing yt-dlp, open a new PowerShell window and re-run this installer." -ForegroundColor Yellow
    return
}

$ytDlpDir = Join-Path $env:APPDATA "yt-dlp"
$zipUrl = "https://github.com/toml12791/Clean-Auto-Subs/releases/latest/download/Clean-Auto-Subs.zip"
$tempZip = Join-Path $env:TEMP "Clean-Auto-Subs.zip"
$tempExtract = Join-Path $env:TEMP "Clean-Auto-Subs-extract"

$configPath = Join-Path $ytDlpDir "config.txt"
$backupPath = Join-Path $ytDlpDir "config.bak.txt"

$captionBlock = @"

# Clean & embed English automatic captions
--write-auto-subs
--sub-langs "en-orig"
--sub-format "vtt"
--use-postprocessor "CleanAutoSubs:when=before_dl"
--ppa 'EmbedSubtitle+ffmpeg_o:-metadata:s:s:0 title="Cleaned Auto Captions" -disposition:s:0 default'
--embed-subs
"@

Write-Host "Installing Clean-Auto-Subs to $ytDlpDir..." -ForegroundColor Cyan

New-Item -ItemType Directory -Force -Path $ytDlpDir | Out-Null
Invoke-WebRequest -Uri $zipUrl -OutFile $tempZip
if (Test-Path $tempExtract) { Remove-Item $tempExtract -Recurse -Force }
Expand-Archive -Path $tempZip -DestinationPath $tempExtract -Force

Copy-Item -Path (Join-Path $tempExtract "plugins") -Destination $ytDlpDir -Recurse -Force
Write-Host "Installed Clean-Auto-Subs plugin files." -ForegroundColor Green

if (Test-Path $configPath) {
    Copy-Item -Path $configPath -Destination $backupPath -Force
    Write-Host "Backed up existing config.txt to config.bak.txt." -ForegroundColor Yellow

    $rawConfig = Get-Content -Path $configPath -Raw
    if ($rawConfig -notmatch "CleanAutoSubs") {
        $lines = Get-Content -Path $configPath
        $conflictPattern = '^\s*(--(write-auto-subs|no-write-auto-subs|sub-langs|sub-format|embed-subs|no-embed-subs)\b|--(ppa|postprocessor-args)\s+["'']?EmbedSubtitle)'
        $replacedCount = 0

        $cleanedLines = foreach ($line in $lines) {
            if ($line -match $conflictPattern) {
                $replacedCount++
                "# [Replaced by Clean-Auto-Subs] $line"
            } else {
                $line
            }
        }

        Set-Content -Path $configPath -Value $cleanedLines -Encoding UTF8
        Add-Content -Path $configPath -Value $captionBlock -Encoding UTF8

        if ($replacedCount -gt 0) {
            Write-Host "Commented out $replacedCount conflicting subtitle line(s) and added CleanAutoSubs settings." -ForegroundColor Green
        } else {
            Write-Host "Appended CleanAutoSubs caption settings to your existing config.txt." -ForegroundColor Green
        }
    } else {
        Write-Host "config.txt already contains CleanAutoSubs settings; skipped duplicate append." -ForegroundColor Yellow
    }
} else {
    Copy-Item -Path (Join-Path $tempExtract "config.txt") -Destination $configPath -Force
    Write-Host "Installed starter config.txt." -ForegroundColor Green
    Write-Host "IMPORTANT: Open $configPath and update the 'D:/YouTube' paths to match your preferred folder." -ForegroundColor Yellow
}

Remove-Item -Path $tempZip -Force -ErrorAction SilentlyContinue
Remove-Item -Path $tempExtract -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "`nVerifying installation..." -ForegroundColor Cyan
$debugOutput = (yt-dlp -v -s 2>&1 | Out-String)
if ($debugOutput -match "CleanAutoSubs") {
    Write-Host "Success! yt-dlp has detected and loaded CleanAutoSubs." -ForegroundColor Green
} else {
    Write-Host "Warning: yt-dlp ran, but CleanAutoSubs was not listed in loaded plugins. Check $ytDlpDir\plugins." -ForegroundColor Yellow
}
