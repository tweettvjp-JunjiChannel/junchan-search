"""
メディア インデックス検索システム
- ローカルの動画・画像ファイルをフォルダスキャンして SQLite に登録
- 日本語ファイル名 → タイトルとして自動登録
- 英数字ランダム名 (IMG_xxxx, Gj9thj2bMAAhKIK など) → タイトル未設定として保留
- あいまい検索・キーワードハイライト・画像プレビュー・動画起動
- タイトル未設定ファイルを一覧表示してリネーム（DB + 実ファイル同時更新）
- 管理者認証あり（スキャン・リネームは管理者のみ）
"""

import streamlit as st
import sqlite3
import os
import re
import hmac
import html as html_lib
import pathlib

# ─────────────────────────────────────────────────────────────────────────────
# 定数・正規表現
# ─────────────────────────────────────────────────────────────────────────────

DB_PATH = "media_index.db"

MEDIA_EXTS = frozenset({
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv",          # 動画
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff",  # 画像
})
IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"})
VIDEO_EXTS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv"})

# 日本語（ひらがな・カタカナ・漢字・全角記号）を含むか
_JP_RE = re.compile(
    r'[぀-ゟ゠-ヿ一-鿿㐀-䶿'
    r'豈-﫿！-｠　-〿]'
)

# カメラ・スマホ・ドローンの自動採番パターン
_CAM_RE = re.compile(
    r'^(IMG|PHOTO|PIC|PICT|DSC[NFO]?|MVI|VID|VIDEO|MOV|DCIM|'
    r'RIMG|SANY|IMGP|EX\d|P\d{4}|DJI|GH\d{1,2}|GOPR|GP\d+|'
    r'BURST|SNAP|SCREENSHOT|Screenshot|screen|Screen)[\d_\-\s]',
    re.IGNORECASE,
)

# Twitter / SNS 等のランダム英数字（スペースなし、8 文字以上）
_RANDOM_RE = re.compile(r'^[A-Za-z0-9_\-]{8,}$')

# Windows ファイル名禁止文字
_INVALID_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1F]')


# ─────────────────────────────────────────────────────────────────────────────
# ファイル名分類
# ─────────────────────────────────────────────────────────────────────────────

def _classify_stem(stem: str) -> tuple[str, bool]:
    """
    ファイル名のステム（拡張子なし）を分類して (登録タイトル, ランダム名フラグ) を返す。

    - 日本語あり           → (stem, False)  ← そのままタイトルとして使用
    - カメラ自動採番パターン  → ('', True)    ← タイトル未設定として保留
    - ランダム英数字         → ('', True)    ← タイトル未設定として保留
    - それ以外（英語など）    → (stem, False)  ← 英語タイトルとして使用
    """
    if _JP_RE.search(stem):
        return stem, False
    if _CAM_RE.match(stem):
        return '', True
    if _RANDOM_RE.match(stem):
        return '', True
    return stem, False


# ─────────────────────────────────────────────────────────────────────────────
# データベース操作
# ─────────────────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS media (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            path       TEXT UNIQUE NOT NULL,
            filename   TEXT NOT NULL,
            ext        TEXT NOT NULL,
            title      TEXT    DEFAULT '',
            is_random  INTEGER DEFAULT 0,
            folder     TEXT    DEFAULT '',
            file_size  INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def upsert_media(path: str, filename: str, ext: str, title: str,
                  is_random: int, folder: str, file_size: int) -> str:
    """INSERT または UPDATE。戻り値: 'added' | 'updated'"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id FROM media WHERE path = ?", (path,))
    exists = cur.fetchone()
    if exists:
        conn.execute("""
            UPDATE media
            SET filename=?, ext=?, title=?, is_random=?, folder=?, file_size=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE path=?
        """, (filename, ext, title, is_random, folder, file_size, path))
        result = "updated"
    else:
        conn.execute("""
            INSERT INTO media (path, filename, ext, title, is_random, folder, file_size)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (path, filename, ext, title, is_random, folder, file_size))
        result = "added"
    conn.commit()
    conn.close()
    return result


def search_media(query: str, limit: int = 200) -> list[tuple]:
    """タイトルが設定されているメディアを LIKE 検索（新着順）"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT path, title, ext, file_size, filename
        FROM media
        WHERE title != '' AND title LIKE ?
        ORDER BY updated_at DESC
        LIMIT ?
    """, (f"%{query}%", limit))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_untitled(limit: int = 50) -> list[tuple]:
    """タイトル未設定（ランダム名）のメディアを返す"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT path, filename, ext, file_size
        FROM media
        WHERE title = '' AND is_random = 1
        ORDER BY updated_at DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def update_title_and_rename(old_path: str, new_title: str) -> tuple[bool, str, str]:
    """
    タイトルを DB に保存し、実際のファイルをリネームする。
    Returns: (success, new_path, message)
    """
    p = pathlib.Path(old_path)
    if not p.exists():
        return False, old_path, f"ファイルが見つかりません: {old_path}"

    # Windows 禁止文字を置換
    safe_title = _INVALID_CHARS_RE.sub('_', new_title).strip().strip('.')
    if not safe_title:
        return False, old_path, "タイトルが空です"

    new_name = safe_title + p.suffix
    new_path = str(p.parent / new_name)

    # 大文字小文字を無視して衝突確認
    if new_path.lower() != old_path.lower() and pathlib.Path(new_path).exists():
        return False, old_path, f"同名ファイルが既に存在します: {new_name}"

    # ファイルをリネーム
    try:
        if new_path.lower() != old_path.lower():
            os.rename(old_path, new_path)
    except OSError as e:
        return False, old_path, f"リネーム失敗: {e}"

    # DB を更新
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE media
        SET path=?, filename=?, title=?, is_random=0, updated_at=CURRENT_TIMESTAMP
        WHERE path=?
    """, (new_path, new_name, new_title, old_path))
    conn.commit()
    conn.close()

    return True, new_path, f"✅ {p.name} → {new_name}"


def skip_title(path: str):
    """スキップ：元のファイル名ステムをそのままタイトルにして検索対象にする"""
    stem = pathlib.Path(path).stem
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE media SET is_random=0, title=?, updated_at=CURRENT_TIMESTAMP
        WHERE path=?
    """, (stem, path))
    conn.commit()
    conn.close()


def delete_media_record(path: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM media WHERE path=?", (path,))
    conn.commit()
    conn.close()


def delete_all_media():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM media")
    conn.commit()
    conn.close()


def count_media() -> dict[str, int]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM media")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM media WHERE title = '' AND is_random = 1")
    untitled = cur.fetchone()[0]
    conn.close()
    return {"total": total, "untitled": untitled, "titled": total - untitled}


# ─────────────────────────────────────────────────────────────────────────────
# フォルダスキャン
# ─────────────────────────────────────────────────────────────────────────────

def scan_folder(folder_path: str, prog_bar=None) -> dict:
    """
    フォルダを再帰スキャンして DB に登録する。
    prog_bar: st.progress() オブジェクト（任意）
    """
    folder = pathlib.Path(folder_path)
    if not folder.exists() or not folder.is_dir():
        return {"error": f"フォルダが存在しません: {folder_path}"}

    # ファイル一覧を先に収集（進捗計算のため）
    files = [
        fp for fp in folder.rglob("*")
        if fp.is_file() and fp.suffix.lower() in MEDIA_EXTS
    ]

    total = len(files)
    added = updated = 0

    for i, fp in enumerate(files):
        if prog_bar is not None:
            pct = (i + 1) / max(total, 1)
            prog_bar.progress(min(pct, 1.0), text=f"({i+1}/{total}) {fp.name}")

        stem = fp.stem
        ext = fp.suffix.lower()
        title, is_random = _classify_stem(stem)

        try:
            file_size = fp.stat().st_size
        except OSError:
            file_size = 0

        r = upsert_media(
            path=str(fp),
            filename=fp.name,
            ext=ext,
            title=title,
            is_random=int(is_random),
            folder=str(fp.parent),
            file_size=file_size,
        )
        if r == "added":
            added += 1
        else:
            updated += 1

    return {"total": total, "added": added, "updated": updated}


# ─────────────────────────────────────────────────────────────────────────────
# 管理者認証
# ─────────────────────────────────────────────────────────────────────────────

def _verify_password(password: str) -> bool:
    """st.secrets の ADMIN_PASSWORD と定数時間比較（タイミング攻撃対策）"""
    try:
        correct = st.secrets["ADMIN_PASSWORD"]
    except (KeyError, FileNotFoundError):
        return False
    return hmac.compare_digest(correct.encode("utf-8"), password.encode("utf-8"))


def _is_admin() -> bool:
    return bool(st.session_state.get("is_admin", False))


# ─────────────────────────────────────────────────────────────────────────────
# UI ユーティリティ
# ─────────────────────────────────────────────────────────────────────────────

def _highlight(text: str, query: str) -> str:
    """キーワードを <mark> タグでハイライト（XSS 安全）"""
    if not query:
        return html_lib.escape(text)
    escaped = html_lib.escape(text)
    pattern = re.escape(html_lib.escape(query))
    return re.sub(f"({pattern})", r"<mark>\1</mark>", escaped, flags=re.IGNORECASE)


def _fmt_size(size: int) -> str:
    if size >= 1 << 30:
        return f"{size / (1 << 30):.1f} GB"
    if size >= 1 << 20:
        return f"{size / (1 << 20):.1f} MB"
    if size >= 1 << 10:
        return f"{size / (1 << 10):.1f} KB"
    return f"{size} B"


def _safe_image(path: str, width: int = 300, caption: str = ""):
    """バイト読み込みでローカル画像を安全に表示（Windows パス互換）"""
    try:
        with open(path, "rb") as f:
            data = f.read()
        st.image(data, caption=caption or None, width=width)
    except Exception as e:
        st.caption(f"⚠️ プレビュー不可: {e}")


def _open_file(path: str):
    """OS のデフォルトアプリでファイルを開く（Windows: os.startfile）"""
    try:
        os.startfile(path)  # type: ignore[attr-defined]
    except AttributeError:
        # Windows 以外の場合のフォールバック
        import subprocess
        subprocess.run(["xdg-open", path], check=False)
    except Exception as e:
        st.error(f"ファイルを開けませんでした: {e}")


def _render_media_row(
    path: str, title: str, ext: str, file_size: int,
    highlight_query: str = "", show_delete: bool = False,
):
    """検索結果 1 件のカード（ハイライト + プレビュー + 削除ボタン）を描画"""
    is_image = ext in IMAGE_EXTS
    is_video = ext in VIDEO_EXTS
    p = pathlib.Path(path)
    exists = p.exists()

    display_title = _highlight(title or p.name, highlight_query)
    badge = ext.upper().lstrip(".")
    size_str = _fmt_size(file_size) if file_size else ""
    absent = " &nbsp;⚠️<em>ファイル不在</em>" if not exists else ""
    icon = "🖼️" if is_image else ("🎬" if is_video else "📄")

    col_icon, col_card, col_del = st.columns([1, 20, 1])
    with col_icon:
        st.markdown(
            f"<div style='padding-top:10px;font-size:1.3rem'>{icon}</div>",
            unsafe_allow_html=True,
        )
    with col_card:
        meta = f" [{badge}]" + (f" · {size_str}" if size_str else "") + absent
        st.markdown(
            f'<div class="mc">'
            f'<span class="mc-title">{display_title}</span>'
            f'<span class="mc-meta">{meta}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
    with col_del:
        if show_delete:
            key = f"del_{abs(hash(path)) % 10**12}"
            if st.button("🗑️", key=key, help="DB から削除（実ファイルは消えません）"):
                delete_media_record(path)
                st.rerun()

    # ── プレビュー ──
    if exists:
        if is_image:
            with st.expander("🖼️ 画像プレビュー", expanded=False):
                _safe_image(path, width=420)
                st.caption(f"`{path}`")
        elif is_video:
            col_btn, _ = st.columns([3, 9])
            with col_btn:
                key = f"play_{abs(hash(path)) % 10**12}"
                if st.button("▶ 動画を開く", key=key, use_container_width=True):
                    _open_file(path)
            st.caption(f"`{path}`")
    else:
        st.caption(f"パス: `{path}`")


# ─────────────────────────────────────────────────────────────────────────────
# ページ設定・グローバル CSS
# ─────────────────────────────────────────────────────────────────────────────

init_db()

st.set_page_config(page_title="メディア検索システム", page_icon="🎞️", layout="wide")
st.title("🎞️ メディア インデックス検索")

st.markdown("""
<style>
.mc {
    background: #f8f9fa;
    border-left: 4px solid #4a9eff;
    border-radius: 4px;
    padding: 8px 14px;
    margin-bottom: 2px;
    line-height: 1.7;
}
.mc-title { font-size: 0.95rem; color: #1a1a1a; font-weight: 500; word-break: break-all; }
.mc-meta  { font-size: 0.78rem; color: #888; margin-left: 4px; }
mark {
    background: linear-gradient(transparent 40%, #FFD700 40%);
    color: inherit;
    padding: 0 1px;
    border-radius: 2px;
    font-weight: bold;
}
</style>
""", unsafe_allow_html=True)

# ── サイドバー：管理者ログイン ＆ 統計 ─────────────────────────────────────────
with st.sidebar:
    st.markdown("### 🔐 管理者ログイン")
    if st.session_state.get("is_admin"):
        st.success("✅ 管理者としてログイン中")
        if st.button("ログアウト", key="admin_logout"):
            st.session_state["is_admin"] = False
            st.rerun()
    else:
        _pw = st.text_input("パスワード", type="password", key="admin_pw")
        if st.button("ログイン", key="admin_login"):
            if _verify_password(_pw):
                st.session_state["is_admin"] = True
                st.rerun()
            else:
                st.error("パスワードが違います")

    st.divider()
    _counts = count_media()
    st.metric("総登録数",     f"{_counts['total']:,} 件")
    st.metric("タイトル付き", f"{_counts['titled']:,} 件")
    st.metric("タイトル未設定", f"{_counts['untitled']:,} 件")

# ── タブ構成（管理者のみ スキャン・タイトル設定タブを表示） ─────────────────────
if _is_admin():
    tab_search, tab_scan, tab_rename = st.tabs(["🔍 検索", "📂 スキャン", "🏷️ タイトル設定"])
else:
    _tabs = st.tabs(["🔍 検索"])
    tab_search, tab_scan, tab_rename = _tabs[0], None, None


# ─────────────────────────────────────────────────────────────────────────────
# タブ①：検索
# ─────────────────────────────────────────────────────────────────────────────
with tab_search:
    counts = count_media()
    st.caption(
        f"登録件数: **{counts['total']:,}** 件 "
        f"（検索対象: {counts['titled']:,} 件 ／ タイトル未設定: {counts['untitled']:,} 件）"
    )

    query = st.text_input(
        "キーワードで検索（部分一致・日本語対応）",
        placeholder="例：ワクチン　マスコミ　選挙　陰謀　バチカン　など",
        key="search_query",
    )

    if query.strip():
        rows = search_media(query.strip())
        st.write(f"**{len(rows):,} 件**ヒット")
        if rows:
            for path, title, ext, file_size, filename in rows:
                _render_media_row(
                    path, title, ext, file_size,
                    highlight_query=query.strip(),
                    show_delete=_is_admin(),
                )
        else:
            st.info("一致するメディアが見つかりませんでした。")
    else:
        if counts["total"] == 0:
            st.info(
                "まだメディアが登録されていません。"
                "管理者ログイン後に「📂 スキャン」タブからフォルダを登録してください。"
            )
        else:
            st.info("上の検索窓にキーワードを入力してください。")


# ─────────────────────────────────────────────────────────────────────────────
# タブ②：スキャン（管理者のみ）
# ─────────────────────────────────────────────────────────────────────────────
if tab_scan is not None:
    with tab_scan:
        st.subheader("📂 ローカルフォルダをスキャンして一括登録")
        st.caption(
            "指定フォルダ配下の動画・画像ファイルを再帰スキャンし、データベースに登録します。\n\n"
            "- **日本語ファイル名** → タイトルとして自動登録（すぐに検索可能）\n"
            "- **IMG_xxxx, Gj9thj2bMAAhKIK など** → タイトル未設定として保留"
            "（「🏷️ タイトル設定」タブで後から名前をつけられます）"
        )

        folder_input = st.text_input(
            "スキャンするフォルダのフルパス",
            placeholder=r"例: E:\PublicTweetTV_YouTube",
            key="scan_folder_input",
        )

        col_btn, col_stat = st.columns([3, 4])
        with col_btn:
            do_scan = st.button(
                "🔍 スキャン開始", type="primary",
                key="btn_scan", use_container_width=True,
            )
        with col_stat:
            st.metric("現在の総登録数", f"{count_media()['total']:,} 件")

        if do_scan:
            fpath = folder_input.strip()
            if not fpath:
                st.error("フォルダパスを入力してください。")
            elif not pathlib.Path(fpath).exists():
                st.error(f"フォルダが見つかりません: {fpath}")
            else:
                prog_bar = st.progress(0.0, text="スキャン準備中...")
                result = scan_folder(fpath, prog_bar=prog_bar)
                prog_bar.empty()

                if "error" in result:
                    st.error(result["error"])
                else:
                    st.success(
                        f"✅ スキャン完了 — "
                        f"検出: **{result['total']:,}** 件 ／ "
                        f"新規: **{result['added']:,}** 件 ／ "
                        f"更新: **{result['updated']:,}** 件"
                    )
                    st.rerun()

        st.divider()
        st.markdown("#### ⚠️ データベース全件削除（初期化）")
        st.warning("この操作は取り消せません。実ファイルは削除されません。")
        confirm_all = st.checkbox("全データを削除することを確認しました", key="chk_del_all")
        if st.button(
            "🗑️ 全データを削除する", type="primary",
            disabled=not confirm_all, key="btn_del_all",
        ):
            delete_all_media()
            st.success("全データを削除しました。")
            st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# タブ③：タイトル設定（管理者のみ）
# ─────────────────────────────────────────────────────────────────────────────
if tab_rename is not None:
    with tab_rename:
        st.subheader("🏷️ タイトル未設定メディアの整理")
        st.caption(
            "IMG_xxxx・Gj9thj2bMAAhKIK などランダム名ファイルの一覧です。\n"
            "画像はプレビュー表示。日本語タイトルを入力して「✅ 確定」を押すと、"
            "**DB のタイトルと実際のファイル名を同時に更新**します。"
        )

        untitled_rows = get_untitled(limit=50)
        untitled_count = count_media()["untitled"]

        if not untitled_rows:
            st.success("🎉 タイトル未設定のメディアはありません。")
        else:
            st.info(
                f"タイトル未設定: **{untitled_count:,} 件**（最大50件表示）"
            )

            for i, (path, filename, ext, file_size) in enumerate(untitled_rows):
                st.markdown("---")
                p = pathlib.Path(path)
                is_image = ext in IMAGE_EXTS
                is_video = ext in VIDEO_EXTS
                exists = p.exists()

                col_prev, col_form = st.columns([2, 3])

                # ── 左列：プレビュー ──
                with col_prev:
                    if is_image and exists:
                        _safe_image(path, width=260, caption=filename)
                    elif is_video:
                        st.markdown(f"🎬 **{filename}**")
                        if exists:
                            key_v = f"openv_{i}_{abs(hash(path)) % 10**9}"
                            if st.button("▶ 動画を開く", key=key_v):
                                _open_file(path)
                    else:
                        st.markdown(f"📄 **{filename}**")

                    if file_size:
                        st.caption(f"📦 {_fmt_size(file_size)}")
                    if not exists:
                        st.warning("⚠️ ファイルが見つかりません")
                    st.caption(f"📁 `{path}`")

                # ── 右列：入力フォーム ──
                with col_form:
                    st.markdown(f"**現在のファイル名:** `{filename}`")

                    key_base = f"{i}_{abs(hash(path)) % 10**9}"
                    new_title = st.text_input(
                        "新しいタイトル（ファイル名になります）",
                        placeholder="例：バチカン聖職者の狂気",
                        key=f"ntitle_{key_base}",
                    )

                    col_ok, col_skip, col_del = st.columns([4, 3, 1])

                    with col_ok:
                        if st.button(
                            "✅ 確定（リネーム）",
                            key=f"ok_{key_base}",
                            disabled=not new_title.strip(),
                            use_container_width=True,
                        ):
                            ok, new_path, msg = update_title_and_rename(
                                path, new_title.strip()
                            )
                            if ok:
                                st.success(msg)
                            else:
                                st.error(msg)
                            st.rerun()

                    with col_skip:
                        if st.button(
                            "⏭️ スキップ",
                            key=f"skip_{key_base}",
                            help="元のファイル名をそのままタイトルにして検索対象にする",
                            use_container_width=True,
                        ):
                            skip_title(path)
                            st.rerun()

                    with col_del:
                        if st.button(
                            "🗑️",
                            key=f"del_{key_base}",
                            help="DB から削除（実ファイルは消えません）",
                        ):
                            delete_media_record(path)
                            st.rerun()
