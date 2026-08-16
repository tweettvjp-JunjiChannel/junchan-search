"""
「ニュース」カテゴリー復活対応（2026-08-16）の一環。

タイトル末尾が「…8/12」「...８／１２」のように、省略記号（"…" または "..."）
に続けて「月/日」形式の日付で終わっている記事は、note側で編集部が日々更新する
実質的な「ニュース」記事であるため、既存の「note」カテゴリーは維持したまま
「ニュース」カテゴリー（ID 2448）を追加で付与する一括バックフィル。

判定に使うタイトルは post_title ではなく note_full_title（カスタムフィールド。
post_titleは95文字+"..."に切り詰められているため、末尾の日付部分が切り詰めで
失われている可能性がある）を優先する。note_full_titleが無い記事のみ post_title
にフォールバックする。判定ロジックは custom-search-filter.php の
title_looks_like_news()（save_postフック側）と完全に同じ正規表現を使うこと
（判定基準がズレると「バックフィルでは付いたのに新規記事では付かない」等の
不整合が起きるため、この正規表現を変更する場合は両方を同時に直すこと）。

デフォルトはドライラン（何も変更しない。対象一覧の表示のみ）。
実際にWordPressへ反映するには --execute を指定する。

【チェックポイント設計（順ちゃんAI自律運用ルール準拠）】
--execute実行時、1件反映するたびに news_backfill_progress.json へその記事IDを
即座に追記保存する（バッチ末尾でまとめて書くのではなく、1件ごとに書き切る）。
途中で通信断・junchan_agent.pyによる緊急停止・強制終了等が発生しても、
再実行時は既に成功記録がある記事をスキップして未処理分だけを再開できる。
なお対象抽出自体もWordPress側の実カテゴリー付与状況（NEWS_CATEGORY_IDが
categoriesに含まれるか）を都度確認しているため、この進捗ファイルが無い/古い
状態で再実行しても二重付与が起きることはない（進捗ファイルは主に「どこまで
処理したか」を高速に把握するための補助的な記録であり、正はWordPress側）。

実行方法:
    python backfill_news_category.py                       # ドライラン
    python backfill_news_category.py --execute              # 本番実行（中断時は再実行で再開）
    python backfill_news_category.py --execute --limit 5    # 試験実行（先頭5件のみ）
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

NEWS_CATEGORY_ID = 2448

# 省略記号（"…" or "...")、空白、月、区切り("/"または全角"／")、空白、日、
# （空白）で終わる、という並びを全角/半角数字・空白混在に対応して判定する。
# custom-search-filter.php の title_looks_like_news() と同一ロジック。
NEWS_TITLE_PATTERN = re.compile(
    r"(?:\.\.\.|…)\s*[0-9０-９]{1,2}\s*[/／]\s*[0-9０-９]{1,2}\s*$"
)

SCRIPT_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = SCRIPT_DIR / "wp_credentials.json"
LOG_PATH = SCRIPT_DIR / "backfill_news_category_log.csv"
PROGRESS_PATH = SCRIPT_DIR / "news_backfill_progress.json"
REQUEST_DELAY_SECONDS = 0.5


def load_progress() -> dict:
    if not PROGRESS_PATH.exists():
        return {}
    try:
        return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_progress(progress: dict) -> None:
    PROGRESS_PATH.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")


def load_credentials() -> dict:
    if not CREDENTIALS_PATH.exists():
        raise SystemExit(f"[エラー] {CREDENTIALS_PATH} が見つかりません。")
    data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    for key in ("site_url", "username", "application_password"):
        if not data.get(key) or "ここに" in data[key]:
            raise SystemExit(f"[エラー] wp_credentials.json の '{key}' が未設定です。")
    return data


class WP:
    def __init__(self, site_url: str, username: str, app_password: str):
        self.site_url = site_url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = (username, app_password)

    def list_all_posts(self) -> list[dict]:
        posts: list[dict] = []
        page = 1
        while True:
            r = self.session.get(
                f"{self.site_url}/wp-json/wp/v2/posts",
                params={
                    "per_page": 100,
                    "page": page,
                    "orderby": "id",
                    "order": "asc",
                    "status": "publish,future,draft,pending,private",
                    "context": "edit",
                    "_fields": "id,slug,link,title,categories,meta",
                },
                timeout=30,
            )
            if r.status_code == 400:
                break
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            posts.extend(batch)
            total_pages = int(r.headers.get("X-WP-TotalPages", "1"))
            if page >= total_pages:
                break
            page += 1
        return posts

    def update_categories(self, post_id: int, categories: list[int]) -> dict:
        r = self.session.post(
            f"{self.site_url}/wp-json/wp/v2/posts/{post_id}",
            json={"categories": categories},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


def effective_title(post: dict) -> str:
    meta = post.get("meta") or {}
    full_title = meta.get("note_full_title") or ""
    if full_title.strip():
        return full_title
    title = post.get("title")
    return title["raw"] if isinstance(title, dict) else str(title or "")


def append_log(rows: list[dict]) -> None:
    if not rows:
        return
    write_header = not LOG_PATH.exists()
    with LOG_PATH.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=["timestamp", "post_id", "slug", "title", "before", "after", "result", "detail"]
        )
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="タイトル末尾が「…M/D」形式の記事へニュースカテゴリーを一括付与するバックフィル")
    parser.add_argument("--execute", action="store_true", help="実際にWordPressへ反映する（指定しない場合はドライラン）")
    parser.add_argument("--limit", type=int, default=None, help="対象件数の上限（試験実行用）")
    args = parser.parse_args()

    creds = load_credentials()
    wp = WP(creds["site_url"], creds["username"], creds["application_password"])

    mode = "本番実行（--execute）" if args.execute else "ドライラン（変更なし）"
    print(f"実行モード: {mode}")

    progress = load_progress()
    if progress:
        print(f"進捗ファイルを検出: {PROGRESS_PATH}（記録済み{len(progress)}件はスキップ判定に使用）")

    print("全投稿を走査中...")
    posts = wp.list_all_posts()
    print(f"全投稿: {len(posts)}件")

    targets = []
    skipped_by_progress = 0
    for p in posts:
        title = effective_title(p)
        if not NEWS_TITLE_PATTERN.search(title):
            continue
        if progress.get(str(p["id"]), {}).get("status") == "success":
            skipped_by_progress += 1
            continue  # 進捗ファイルで処理済みと記録済み（中断からの再開時に高速スキップ）
        categories = p.get("categories") or []
        if NEWS_CATEGORY_ID in categories:
            continue  # WordPress側では既に付与済み（進捗ファイルが無い/古い場合の正の情報源）
        targets.append((p, title))

    if skipped_by_progress:
        print(f"進捗ファイルにより{skipped_by_progress}件をスキップしました（既に処理済み）。")
    print(f"ニュースカテゴリー付与対象: {len(targets)}件")

    if args.limit:
        targets = targets[: args.limit]

    log_rows: list[dict] = []
    fixed_count = 0

    for i, (p, title) in enumerate(targets, start=1):
        before = p.get("categories") or []
        after = sorted(set(before) | {NEWS_CATEGORY_ID})

        print(f"[{i}/{len(targets)}] id={p['id']} {p['slug']}: {before} -> {after}  {title[:50]}")

        if not args.execute:
            log_rows.append(
                {
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "post_id": p["id"],
                    "slug": p["slug"],
                    "title": title[:100],
                    "before": before,
                    "after": after,
                    "result": "dry_run_would_fix",
                    "detail": "",
                }
            )
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        try:
            wp.update_categories(p["id"], after)
            fixed_count += 1
            print("    [OK] 付与しました")
            progress[str(p["id"])] = {
                "slug": p["slug"],
                "status": "success",
                "timestamp": datetime.now().isoformat(timespec="seconds"),
            }
            save_progress(progress)  # 1件ごとに即座に永続化する（バッチ末尾でまとめて書かない）
            log_rows.append(
                {
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "post_id": p["id"],
                    "slug": p["slug"],
                    "title": title[:100],
                    "before": before,
                    "after": after,
                    "result": "success",
                    "detail": "",
                }
            )
        except requests.RequestException as e:
            print(f"    [失敗] {e}")
            log_rows.append(
                {
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "post_id": p["id"],
                    "slug": p["slug"],
                    "title": title[:100],
                    "before": before,
                    "after": after,
                    "result": "failed",
                    "detail": str(e)[:200],
                }
            )
        time.sleep(REQUEST_DELAY_SECONDS)

    append_log(log_rows)

    print("\n" + "=" * 60)
    print(f"付与対象: {len(targets)}件 / 付与完了: {fixed_count}件")
    print(f"ログを保存しました: {LOG_PATH}")
    if not args.execute:
        print("これはドライランです。実際には何も変更されていません。--execute を付けて実行すると反映されます。")
    print("=" * 60)


if __name__ == "__main__":
    main()
