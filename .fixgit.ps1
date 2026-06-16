Set-Location 'D:\AI\项目\炒股监控网页\Code\stock-monitor-main'
$backup = Join-Path (Get-Location) '.tmp-backup-rebase'
if (-not (Test-Path $backup)) { New-Item -ItemType Directory -Path $backup | Out-Null }
Get-Item -LiteralPath '.gitignore','.nojekyll','breadth.json','expiration.html','index.html','screening.html' -ErrorAction SilentlyContinue | Move-Item -Destination $backup -Force
& 'D:\Program Files\Git\cmd\git.exe' pull --rebase origin main
& 'D:\Program Files\Git\cmd\git.exe' push origin main
