@echo off
REM note過去記事の差分自動検知・更新バッチ（直近90日以内が対象）。
REM ダブルクリックで本番実行する（note.com側の加筆修正・動画差し替え等を
REM 自動検知し、差分がある記事だけWordPress側を上書き更新する）。
REM 全記事（約360件）をチェックしたい場合は sync_updated_notes.py --all を
REM 手動実行すること（このバッチの対象外）。

cd /d "%~dp0"
python sync_updated_notes.py --execute

echo.
powershell -NoProfile -Command "[Console]::Beep(1000,300); Start-Sleep -Milliseconds 150; [Console]::Beep(1000,300); Start-Sleep -Milliseconds 150; [Console]::Beep(1000,300)"
pause
