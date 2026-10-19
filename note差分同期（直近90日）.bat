@echo off
REM note過去記事の差分自動検知・更新バッチ（直近90日以内のみが対象、軽量版）。
REM ダブルクリックで本番実行する（note.com側の加筆修正・動画差し替え等を
REM 自動検知し、差分がある記事だけWordPress側を上書き更新する）。
REM 数十件程度が対象のため数分で完了する。
REM 全記事（約360件）をチェックしたい場合は「全件差分同期.bat」を使うこと
REM （sync_updated_notes.pyのデフォルトは全件対象のため、こちらは
REM  --recent-days 90 を明示的に指定して軽量モードに固定している）。

cd /d "%~dp0"
python sync_updated_notes.py --execute --recent-days 90

echo.
powershell -NoProfile -Command "[Console]::Beep(1000,300); Start-Sleep -Milliseconds 150; [Console]::Beep(1000,300); Start-Sleep -Milliseconds 150; [Console]::Beep(1000,300)"
pause
