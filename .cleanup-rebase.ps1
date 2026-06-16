$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo
$backup = Join-Path $repo '.tmp-backup-rebase'
if (-not (Test-Path $backup)) { New-Item -ItemType Directory -Path $backup | Out-Null }
Move-Item -LiteralPath '.gitignore','.nojekyll','breadth.json','expiration.html','index.html','screening.html' -Destination $backup -Force
Write-Output "Moved untracked files to $backup"
