@echo off
REM note.com新着記事の週次取り込みバッチ。
REM ダブルクリックで本番実行する（新着記事のダウンロード→WordPress新規投稿→
REM Codoc月額購読プランへの自動紐付けまでを実行）。

cd /d "%~dp0"
python sync_weekly.py --execute
pause
