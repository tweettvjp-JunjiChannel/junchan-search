@echo off
REM note全記事（約360件）を対象にした差分自動検知・更新バッチ。
REM ダブルクリックで本番実行する（note.com側の加筆修正・動画差し替え等を
REM 全件チェックし、差分がある記事だけWordPress側を上書き更新する）。
REM
REM 【重要】全件チェックは実測で約3～3.5時間かかる（note.com側に更新日時
REM メタデータが無いため、差分の有無を判定するにはどのみち全記事の本文を
REM 実際に取得する必要があるため）。メモリ逼迫でプロセスが落ちる事故を
REM 防ぐため、30件ずつプロセスを完全終了させながら繰り返す設計にしてある
REM （sync_updated_notes.py --batch-size）。日常の週次バッチには含めて
REM いない。夜間放置や、まとまった時間が取れるときに実行すること。

cd /d "%~dp0"
set BATCH_NUM=0

:loop
set /a BATCH_NUM+=1
echo =====================================================
echo  バッチ #%BATCH_NUM% 開始
echo =====================================================
python sync_updated_notes.py --execute --batch-size 30
set RC=%ERRORLEVEL%
echo =====================================================
echo  バッチ #%BATCH_NUM% 終了 (exit=%RC%)
echo =====================================================

if %RC%==2 (
    echo 未処理の記事が残っているため次のバッチへ継続します。
    goto loop
)

if %RC%==0 (
    echo 全件差分同期が完了しました。
    powershell -NoProfile -Command "1..3 | ForEach-Object { [Console]::Beep(1000,300); Start-Sleep -Milliseconds 200 }"
    pause
    exit /b 0
)

echo 異常終了を検知しました (exit code=%RC%)
powershell -NoProfile -Command "[Console]::Beep(400,800)"
pause
exit /b 1
