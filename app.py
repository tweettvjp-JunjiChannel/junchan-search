import streamlit as st
import sqlite3
import re
from bs4 import BeautifulSoup
from pathlib import Path

DB_PATH = "notes.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_title ON notes(title)")
    conn.commit()
    conn.close()


def normalize_url(url: str) -> str:
    """Remove trailing slashes and fragments for dedup."""
    return url.strip().rstrip("/").split("#")[0]


def upsert_note(url: str, title: str) -> str:
    url = normalize_url(url)
    title = title.strip()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT title FROM notes WHERE url = ?", (url,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO notes (url, title) VALUES (?, ?)", (url, title))
        result = "added"
    elif row[0] != title:
        cur.execute(
            "UPDATE notes SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE url = ?",
            (title, url),
        )
        result = "updated"
    else:
        result = "skipped"
    conn.commit()
    conn.close()
    return result


def extract_from_html(html: str) -> list[tuple[str, str]]:
    """Extract (url, title) pairs where href contains note.com."""
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    results = []
    for tag in soup.find_all("a", href=True):
        href: str = tag["href"]
        if "note.com" not in href:
            continue
        title = tag.get_text(" ", strip=True)
        if not title:
            continue
        key = normalize_url(href)
        if key in seen:
            continue
        seen.add(key)
        results.append((href, title))
    return results


def search_notes(query: str) -> list[tuple[str, str]]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT url, title FROM notes WHERE title LIKE ? ORDER BY updated_at DESC",
        (f"%{query}%",),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_all_notes(limit: int = 200) -> list[tuple[str, str]]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT url, title FROM notes ORDER BY updated_at DESC LIMIT ?", (limit,)
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def count_notes() -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM notes")
    n = cur.fetchone()[0]
    conn.close()
    return n


# ── 起動時初期化 ─────────────────────────────────────────────────────────────
init_db()

# ── ページ設定 ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="note インデックス検索",
    page_icon="📝",
    layout="wide",
)

st.title("📝 note インデックス検索システム")

# カード風スタイル
st.markdown(
    """
    <style>
    .note-card {
        background: #f8f9fa;
        border-left: 4px solid #41b883;
        border-radius: 4px;
        padding: 10px 14px;
        margin-bottom: 10px;
        line-height: 1.6;
    }
    .note-card a {
        color: #1a1a1a;
        text-decoration: none;
        font-size: 0.95rem;
        word-break: break-all;
    }
    .note-card a:hover {
        color: #41b883;
        text-decoration: underline;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

tab_search, tab_add = st.tabs(["🔍 検索", "➕ データ追加"])

# ────────────────────────────────────────────────────────────────────────────
# タブ①：検索
# ────────────────────────────────────────────────────────────────────────────
with tab_search:
    total = count_notes()
    st.caption(f"登録件数: {total:,} 件")

    query = st.text_input(
        "キーワードで検索（部分一致）",
        placeholder="例：AI　生成　Python　など",
    )

    if query.strip():
        results = search_notes(query.strip())
        st.write(f"**{len(results):,} 件**ヒット")
        if results:
            for url, title in results:
                st.markdown(
                    f'<div class="note-card"><a href="{url}" target="_blank" rel="noopener">{title}</a></div>',
                    unsafe_allow_html=True,
                )
        else:
            st.info("一致する記事が見つかりませんでした。")
    else:
        if total == 0:
            st.info("まだ記事が登録されていません。「データ追加」タブからHTMLを貼り付けて登録してください。")
        else:
            with st.expander(f"全件表示（最新 200 件）", expanded=False):
                for url, title in get_all_notes():
                    st.markdown(
                        f'<div class="note-card"><a href="{url}" target="_blank" rel="noopener">{title}</a></div>',
                        unsafe_allow_html=True,
                    )

# ────────────────────────────────────────────────────────────────────────────
# タブ②：データ追加
# ────────────────────────────────────────────────────────────────────────────
with tab_add:
    st.subheader("エキサイトブログのHTMLから記事を一括登録")
    st.caption(
        "エキサイトブログの記事ページのHTMLソース（全体でも本文のみでも可）を"
        "そのまま貼り付けてください。note.com へのリンクとタイトルを自動抽出します。"
    )

    html_input = st.text_area(
        "HTMLソースをここに貼り付け",
        height=320,
        placeholder='<a href="https://note.com/xxx/n/yyy">記事タイトル</a> が含まれるHTMLを貼り付けてください',
    )

    if st.button("抽出して登録する", type="primary"):
        if not html_input.strip():
            st.error("HTMLを入力してください。")
        else:
            extracted = extract_from_html(html_input)
            if not extracted:
                st.warning(
                    "note.com へのリンクが見つかりませんでした。\n"
                    "ページのHTMLソース（Ctrl+U または右クリック→ページのソースを表示）を貼り付けているか確認してください。"
                )
            else:
                added = updated = skipped = 0
                for url, title in extracted:
                    r = upsert_note(url, title)
                    if r == "added":
                        added += 1
                    elif r == "updated":
                        updated += 1
                    else:
                        skipped += 1

                st.success(
                    f"✅ 処理完了 — 新規追加: {added} 件 ／ 更新: {updated} 件 ／ スキップ（重複）: {skipped} 件"
                )

                with st.expander(f"今回抽出した {len(extracted)} 件を確認"):
                    for url, title in extracted:
                        st.markdown(
                            f'<div class="note-card"><a href="{url}" target="_blank" rel="noopener">{title}</a></div>',
                            unsafe_allow_html=True,
                        )
