@echo off
REM 月次メンテナンスバッチ。
REM ダブルクリックで本番実行する（直近4ヶ月の記事の更新追従＋
REM 公開90日経過記事のCodoc自動値下げ100円までを実行）。

cd /d "%~dp0"
python sync_monthly_maintenance.py --execute
pause
