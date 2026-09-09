"""
note.com由来のハッシュタグ（先頭に「#」「＃」が付いたタグ名）がそのまま
WordPressのタグとして取り込まれ、シャープ無しの正規タグ（例:「気象兵器」）
と「#気象兵器」が別タグとして分断・重複していた不具合の一括クレンジング。

【経緯】auto_sync_blogs.py の fetch_note_article_list() は note.com APIの
hashtags[].hashtag.name をそのままタグ名として使っており（例: "#気象兵器"）、
シャープを含んだ文字列でそのまま wp.get_or_create_tag() へ渡していたため、
過去に手動移行・別経路等でシャープ無しの「気象兵器」タグが既に存在していても
一致判定されず、常に別タグとして新規作成されていた（サイドバーのタグ一覧・
記事の絞り込みの双方で同じ話題の記事が2つのタグに分断される実害があった）。

【対応】
1. 全タグを走査し、シャープ付き名を正規化（先頭の#/＃と前後の空白を除去）
   した名前でグルーピングする。
   - 正規（シャープ無し）タグが既に存在する場合: シャープ付きタグに紐づく
     全投稿へ正規タグを付け替えた上で、シャープ付きタグを削除する（記事の
     所属タグ自体は正規タグへ完全に統合され、件数も自然に合算される）。
   - 正規タグが存在せずシャープ付きタグしか無い場合: そのタグ自体の名前
     から単にシャープを取り除く（投稿の付け替えは不要。タグIDは維持される
     ため既存記事との紐付けはそのまま）。
2. auto_sync_blogs.py 側（wp.get_or_create_tag に渡す前）でもタグ文字列の
   先頭の#/＃・前後の空白を自動除去する正規化を追加済み（本スクリプトとは
   別途、恒久対策として）。これにより今後の同期では二度と分断が発生しない。

【チェックポイント設計について】この処理は「毎回WordPressの現在のタグ一覧を
取得し直し、まだ重複・シャープ付きのまま残っているものだけを対象にする」
設計のため、実行が中断されても、再実行すれば既に処理済み（削除済み・
改名済み）のタグは自然に対象から除外される。専用の進捗ファイルを別途
持たなくても手戻りなく再開できる。

デフォルトはドライラン（何も変更しない）。実際にWordPressへ反映するには
--execute を指定する。

実行方法:
    python backfill_normalize_hashtag_tags.py                  # ドライラン
    python backfill_normalize_hashtag_tags.py --execute         # 本番実行
    python backfill_normalize_hashtag_tags.py --execute --limit 5  # 試験実行
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = SCRIPT_DIR / "wp_credentials.json"
LOG_PATH = SCRIPT_DIR / "backfill_normalize_hashtag_tags_log.csv"
REQUEST_DELAY_SECONDS = 0.3
HASH_PREFIX_CHARS = "#＃"


def load_credentials() -> dict:
    if not CREDENTIALS_PATH.exists():
        raise SystemExit(f"[エラー] {CREDENTIALS_PATH} が見つかりません。")
    data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    for key in ("site_url", "username", "application_password"):
        if not data.get(key) or "ここに" in data[key]:
            raise SystemExit(f"[エラー] wp_credentials.json の '{key}' が未設定です。")
    return data


def normalize_tag_name(name: str) -> str:
    """タグ名の先頭にある#/＃と前後の空白を除去した正規名を返す。"""
    return name.strip().lstrip(HASH_PREFIX_CHARS).strip()


def is_hash_prefixed(name: str) -> bool:
    stripped = name.strip()
    return bool(stripped) and stripped[0] in HASH_PREFIX_CHARS


class WP:
    def __init__(self, site_url: str, username: str, app_password: str):
        self.site_url = site_url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = (username, app_password)

    def list_all_tags(self) -> list[dict]:
        tags: list[dict] = []
        page = 1
        while True:
            r = self.session.get(
                f"{self.site_url}/wp-json/wp/v2/tags",
                params={
                    "per_page": 100, "page": page,
                    "orderby": "id", "order": "asc",
                    "_fields": "id,name,slug,count",
                },
                timeout=30,
            )
            if r.status_code == 400:
                break
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            tags.extend(batch)
            if page >= int(r.headers.get("X-WP-TotalPages", "1")):
                break
            page += 1
        return tags

    def list_posts_for_tag(self, tag_id: int) -> list[dict]:
        posts: list[dict] = []
        page = 1
        while True:
            r = self.session.get(
                f"{self.site_url}/wp-json/wp/v2/posts",
                params={
                    "tags": tag_id, "per_page": 100, "page": page,
                    "orderby": "id", "order": "asc",
                    "status": "publish,future,draft,pending,private",
                    "context": "edit", "_fields": "id,tags",
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
            if page >= int(r.headers.get("X-WP-TotalPages", "1")):
                break
            page += 1
        return posts

    def update_post_tags(self, post_id: int, tags: list[int]) -> dict:
        r = self.session.post(
            f"{self.site_url}/wp-json/wp/v2/posts/{post_id}", json={"tags": tags}, timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def rename_tag(self, tag_id: int, new_name: str) -> dict:
        # slugを空文字で送ると、WordPress側がnameから自動でスラッグを
        # 再生成する（既存スラッグを明示指定しない限り自動追従する挙動を
        # 実機で確認済み）。
        r = self.session.post(
            f"{self.site_url}/wp-json/wp/v2/tags/{tag_id}", json={"name": new_name, "slug": ""}, timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def delete_tag(self, tag_id: int) -> dict:
        r = self.session.delete(
            f"{self.site_url}/wp-json/wp/v2/tags/{tag_id}", params={"force": "true"}, timeout=30,
        )
        r.raise_for_status()
        return r.json()


def append_log(rows: list[dict]) -> None:
    if not rows:
        return
    write_header = not LOG_PATH.exists()
    with LOG_PATH.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=["timestamp", "tag_id", "tag_name", "normalized_name", "action", "result", "detail"]
        )
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def log_row(tag_id, tag_name, normalized_name, action, result, detail="") -> dict:
    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "tag_id": tag_id,
        "tag_name": tag_name,
        "normalized_name": normalized_name,
        "action": action,
        "result": result,
        "detail": detail[:200],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="#/＃付きnoteハッシュタグの重複統合・正規化")
    parser.add_argument("--execute", action="store_true", help="実際にWordPressへ反映する（指定しない場合はドライラン）")
    parser.add_argument("--limit", type=int, default=None, help="処理対象グループ数の上限（試験実行用）")
    args = parser.parse_args()

    creds = load_credentials()
    wp = WP(creds["site_url"], creds["username"], creds["application_password"])

    mode = "本番実行（--execute）" if args.execute else "ドライラン（変更なし）"
    print(f"実行モード: {mode}")

    print("全タグを走査中...")
    tags = wp.list_all_tags()
    print(f"全タグ: {len(tags)}件")

    groups: dict[str, list[dict]] = defaultdict(list)
    for t in tags:
        groups[normalize_tag_name(t["name"])].append(t)

    rename_only: list[tuple[dict, str]] = []       # (tag, normalized_name)
    merge_groups: list[tuple[dict, list[dict], str]] = []  # (canonical, duplicates, normalized_name)

    for norm, entries in groups.items():
        if not norm:
            continue  # シャープのみ等、正規化後に空文字になる異常系は対象外
        if len(entries) == 1:
            t = entries[0]
            if is_hash_prefixed(t["name"]):
                rename_only.append((t, norm))
            continue

        bare = [t for t in entries if t["name"].strip() == norm]
        others = [t for t in entries if t not in bare]
        if bare:
            canonical = bare[0]
            duplicates = others + bare[1:]  # 万一bareが複数あれば2件目以降も統合対象にする
        else:
            # 正規タグが存在しない：先頭のシャープ付きタグを正規タグへ昇格（改名）し、
            # 残りをそこへ統合する。
            canonical = others[0]
            rename_only.append((canonical, norm))
            duplicates = others[1:]
        if duplicates:
            merge_groups.append((canonical, duplicates, norm))

    print(f"改名のみで済むシャープ付きタグ: {len(rename_only)}件")
    print(f"正規タグへ統合が必要なグループ: {len(merge_groups)}件"
          f"（延べ統合対象タグ数: {sum(len(d) for _, d, _ in merge_groups)}件）")

    if args.limit:
        rename_only = rename_only[: args.limit]
        merge_groups = merge_groups[: args.limit]

    log_rows: list[dict] = []

    print("\n" + "=" * 60)
    print("【1】正規タグが存在しないシャープ付きタグの改名")
    print("=" * 60)
    for i, (t, norm) in enumerate(rename_only, start=1):
        print(f"[{i}/{len(rename_only)}] id={t['id']} 「{t['name']}」({t['count']}件) -> 「{norm}」")
        if not args.execute:
            log_rows.append(log_row(t["id"], t["name"], norm, "rename", "dry_run_would_rename"))
            continue
        try:
            wp.rename_tag(t["id"], norm)
            print("    [OK] 改名しました")
            log_rows.append(log_row(t["id"], t["name"], norm, "rename", "success"))
        except requests.RequestException as e:
            print(f"    [失敗] {e}")
            log_rows.append(log_row(t["id"], t["name"], norm, "rename", "failed", str(e)))
        time.sleep(REQUEST_DELAY_SECONDS)

    print("\n" + "=" * 60)
    print("【2】正規タグへの統合（投稿の付け替え → シャープ付きタグの削除）")
    print("=" * 60)
    for gi, (canonical, duplicates, norm) in enumerate(merge_groups, start=1):
        print(f"[{gi}/{len(merge_groups)}] 正規タグ「{norm}」(id={canonical['id']}, 現在{canonical['count']}件) へ統合:")
        for dup in duplicates:
            print(f"    - id={dup['id']} 「{dup['name']}」({dup['count']}件)")
            if not args.execute:
                log_rows.append(log_row(dup["id"], dup["name"], norm, "merge_into_" + str(canonical["id"]), "dry_run_would_merge"))
                continue
            try:
                posts = wp.list_posts_for_tag(dup["id"])
                print(f"      対象投稿: {len(posts)}件")
                for p in posts:
                    current_tags = set(p.get("tags") or [])
                    current_tags.add(canonical["id"])
                    current_tags.discard(dup["id"])
                    wp.update_post_tags(p["id"], sorted(current_tags))
                    time.sleep(REQUEST_DELAY_SECONDS)
                wp.delete_tag(dup["id"])
                print("      [OK] 付け替え・削除しました")
                log_rows.append(
                    log_row(dup["id"], dup["name"], norm, "merge_into_" + str(canonical["id"]), "success",
                            f"reassigned_posts={len(posts)}")
                )
            except requests.RequestException as e:
                print(f"      [失敗] {e}")
                log_rows.append(log_row(dup["id"], dup["name"], norm, "merge_into_" + str(canonical["id"]), "failed", str(e)))
            time.sleep(REQUEST_DELAY_SECONDS)

    append_log(log_rows)

    print("\n" + "=" * 60)
    print(f"改名: {len(rename_only)}件 / 統合グループ: {len(merge_groups)}件")
    print(f"ログを保存しました: {LOG_PATH}")
    if not args.execute:
        print("これはドライランです。実際には何も変更されていません。--execute を付けて実行すると反映されます。")
    print("=" * 60)


if __name__ == "__main__":
    main()
