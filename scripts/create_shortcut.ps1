$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $Desktop "ARONA 微信助手.lnk"
$Target = Join-Path $Root "启动 ARONA.bat"
$Icon = Join-Path $Root "assets\arona_icon.ico"
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Target
$Shortcut.WorkingDirectory = $Root
$Shortcut.IconLocation = "$Icon,0"
$Shortcut.Description = "ARONA · 什亭之匣微信助手"
$Shortcut.Save()
Write-Host "已创建桌面快捷方式：$ShortcutPath" -ForegroundColor Green
