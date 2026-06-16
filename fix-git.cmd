@echo off
cd /d D:\AI\项目\炒股监控网页\Code\stock-monitor-main
if not exist .tmp-backup-rebase mkdir .tmp-backup-rebase
move /Y .gitignore .tmp-backup-rebase
move /Y .nojekyll .tmp-backup-rebase
move /Y breadth.json .tmp-backup-rebase
move /Y expiration.html .tmp-backup-rebase
move /Y index.html .tmp-backup-rebase
move /Y screening.html .tmp-backup-rebase
"D:\Program Files\Git\cmd\git.exe" pull --rebase origin main
"D:\Program Files\Git\cmd\git.exe" push origin main
