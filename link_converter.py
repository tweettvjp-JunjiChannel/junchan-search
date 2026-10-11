"""
note.com からダウンロードした記事HTML（note_downloader.py の出力）を、
独自の読み放題サブスクサイト用に変換するスクリプト。

対象ファイルの見分け方:
    F:\\ClaudeCode 以下を再帰的に走査するが、note_downloader.py が書き出した
    記事ファイル（<meta name="source-url" content="https://note.com/...">を含む）
    だけを変換対象とする。それ以外の HTML（このリポジトリ内の無関係なツール等）は
    自動的にスキップされ、上書きされない。

変換内容:
    1. 内部リンクの置換
       note.com/tweettv/n/{key} 形式のリンクを見つけたら、
       ダウンロード済みであれば同じ converted_html フォルダ内の対応ファイルへの
       相対リンクに、ダウンロードされていなければ /article/{key} という
       汎用パスに置き換える。それ以外（プロフィールURLそのもの等）は "/" にする。
       外部サイトへのリンクは変更しない。
       ※ 本文中の「自分の別記事への関連リンクカード」
       （<figure embedded-service="note" data-src="https://note.com/...">、
       内部で note.com/embed/notes/... のiframeを読み込む仕様）も内部リンクの
       一種として扱い、note.comへの依存が残らないよう、同様にローカルの
       記事へのシンプルなリンクに置き換える。

    2. YouTube埋め込み（エラー153）対策
       note独自の <figure embedded-service="youtube" ...>...</figure> の
       正規の埋め込みに加え、動画を「単なるリンク」として貼ったために
       note側のoEmbed解決が失敗し embedded-service="external-article" 等の
       汎用リンクカード（「- YouTube / Enjoy the videos...」という
       メタデータ文言つきの黒いエラーカードになるケース）も含めて、
       YouTubeへの参照を含む <figure> はすべて検出して動画IDを抽出し、標準的な
       <iframe width="560" height="315" src="https://www.youtube.com/embed/ID"
        frameborder="0" allowfullscreen></iframe>
       単体に（メタデータ文言ごと）置き換える。figureに包まれていない
       裸のiframeやリンクも保険として同様に変換する。

    3. 目次（TOC）の再構築
       note独自の <nav class="o-tableOfContents"> は、10項目を超える分が
       「すべて表示」ボタンの裏でJSにより後から表示される仕様のため、
       ダウンロード済みHTMLには全項目が入っていないことがある。
       そのため元のnavは削除し、本文中の全 h2/h3 見出し（既にユニークな
       id属性を持っている）から目次を新規に組み立て直す。
       各項目は <a href="#見出しのid"> のシンプルな静的アンカーリンクになる。

    4. 本文冒頭のタイトル重複除去
       note_downloader.py が付与する <h1>タイトル</h1><p><a>出典URL</a></p><hr>
       を本文から取り除く。タイトルは別途メタデータで保持しているため、本文に
       残したままだとWordPress等へのインポート時に投稿タイトルと二重表示になる。

    5. 外部サイトへのリンクカードのクレンジング
       note編集画面でURLをそのまま貼ると自動生成される、タイトル・説明・
       ドメインが枠で囲まれたプレビューカード（class="external-article-widget"。
       Twitcastingの入会案内など）を、枠の無いシンプルなテキストリンクに変換する。
       YouTube／自記事へのリンクカードは1・2の処理で個別に変換済みのため、
       ここで対象になるのはそれ以外の外部サイトへのリンクカードのみ。

実行方法:
    pip install beautifulsoup4
    python link_converter.py
    # 入出力先を変える場合:
    python link_converter.py --input-dir "F:\\ClaudeCode" --output-dir "F:\\ClaudeCode\\converted_html"
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from bs4 import BeautifulSoup, Tag

USERNAME = "tweettv"
SCRIPT_DIR = Path(__file__).resolve().parent

SOURCE_URL_META_RE = re.compile(r'<meta name="source-url" content="([^"]+)"')
TITLE_TAG_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
INTERNAL_KEY_RE = re.compile(rf"note\.com/{re.escape(USERNAME)}/n/([a-zA-Z0-9]+)")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtube-nocookie.com", "www.youtube-nocookie.com"}
YOUTUBE_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="note.com 記事HTMLをサブスクサイト用に一括変換")
    parser.add_argument("--input-dir", default=str(SCRIPT_DIR), help="変換元HTMLを探すフォルダ（既定: このスクリプトのあるフォルダ）")
    parser.add_argument("--output-dir", default=str(SCRIPT_DIR / "converted_html"), help="変換後HTMLの出力先フォルダ")
    parser.add_argument("--force", action="store_true", help="出力先に既に同名ファイルがあっても変換し直す（既定は新規ファイルのみ変換）")
    return parser.parse_args()


def find_article_files(input_dir: Path, output_dir: Path) -> list[tuple[Path, str, str]]:
    """
    input_dir 以下を再帰的に走査し、note_downloader.py が書き出した記事ファイル
    （source-url メタタグを持つもの）だけを (パス, 記事key, タイトル) のリストとして返す。
    """
    articles: list[tuple[Path, str, str]] = []
    output_dir_resolved = output_dir.resolve()

    for path in sorted(input_dir.rglob("*.html")):
        if output_dir_resolved in path.resolve().parents or path.resolve() == output_dir_resolved:
            continue  # 出力先フォルダ自体は対象外

        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue

        m = SOURCE_URL_META_RE.search(text)
        if not m:
            continue  # note記事ではない（無関係なHTML）ためスキップ

        key_m = INTERNAL_KEY_RE.search(m.group(1))
        if not key_m:
            continue

        title_m = TITLE_TAG_RE.search(text)
        title = title_m.group(1).strip() if title_m else key_m.group(1)

        articles.append((path, key_m.group(1), title))

    return articles


def build_key_to_filename_map(articles: list[tuple[Path, str, str]]) -> dict[str, str]:
    """記事key -> 変換後の出力ファイル名（converted_html フォルダ直下でのファイル名）"""
    mapping: dict[str, str] = {}
    used_names: set[str] = set()

    for path, key, _title in articles:
        name = path.name
        if name in used_names and mapping.get(key) != name:
            # 同名ファイルが別の記事として既に使われている場合のみ衝突回避
            name = f"{path.parent.name}__{name}"
        used_names.add(name)
        mapping[key] = name

    return mapping


def build_key_to_title_map(articles: list[tuple[Path, str, str]]) -> dict[str, str]:
    """記事key -> 記事タイトル（関連記事リンクカードの表示テキストに使う）"""
    return {key: title for _path, key, title in articles}


def extract_youtube_id(url: str | None) -> str | None:
    """
    YouTubeのURLから動画IDを抽出する。urlparse/parse_qsで解析するため、
    watch?app=desktop&v=ID のようにクエリパラメータの順序が定型的でない
    URLでも正しく取れる（単純な正規表現の watch\\?v= 決め打ちだと拾えない）。
    """
    if not url:
        return None

    url = url.strip()
    if "://" not in url:
        url = "https://" + url.lstrip("/")

    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    host = parsed.netloc.lower()
    path_parts = [p for p in parsed.path.split("/") if p]

    if host in YOUTUBE_HOSTS:
        qs = parse_qs(parsed.query)
        if qs.get("v"):
            return qs["v"][0]
        for marker in ("embed", "shorts", "live", "v"):
            if marker in path_parts:
                idx = path_parts.index(marker)
                if idx + 1 < len(path_parts):
                    return path_parts[idx + 1]
        return None

    if host in YOUTUBE_SHORT_HOSTS:
        return path_parts[0] if path_parts else None

    return None


def convert_internal_links(soup: BeautifulSoup, key_to_filename: dict[str, str]) -> None:
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if f"note.com/{USERNAME}" not in href:
            continue  # 外部サイト、または他ユーザーの記事へのリンクはそのまま維持

        key_m = INTERNAL_KEY_RE.search(href)
        if key_m:
            key = key_m.group(1)
            if key in key_to_filename:
                a["href"] = quote(key_to_filename[key])
            else:
                # ダウンロードされていない記事（削除済み等）は汎用パスにフォールバック
                a["href"] = f"/article/{key}"
        else:
            # プロフィールURLそのもの等、記事IDを含まない自サイトへのリンク
            a["href"] = "/"


def convert_note_embed_cards(
    soup: BeautifulSoup,
    key_to_filename: dict[str, str],
    key_to_title: dict[str, str],
) -> int:
    """
    「自分の別記事への関連リンクカード」（note.com/embed/notes/... のiframeに
    依存するウィジェット）を、ローカル記事へのシンプルなリンクに置き換える。
    """
    count = 0
    for figure in soup.find_all("figure", attrs={"embedded-service": "note"}):
        key = figure.get("data-identifier") or ""
        if not key:
            key_m = INTERNAL_KEY_RE.search(figure.get("data-src", ""))
            key = key_m.group(1) if key_m else ""
        if not key:
            continue

        if key in key_to_filename:
            target = quote(key_to_filename[key])
        else:
            target = f"/article/{key}"
        title = key_to_title.get(key, target)

        p = soup.new_tag("p", **{"class": "note-embed-link"})
        # 置換元figureのidを引き継ぐ（build_master_index.pyのpaywall_element_idが
        # この要素を指している場合があり、失うと有料壁の挿入位置特定に支障が出るため）
        if figure.get("id"):
            p["id"] = figure["id"]
        a = soup.new_tag("a", href=target)
        a.string = f"→ {title}"
        p.append(a)
        figure.replace_with(p)
        count += 1

    return count


def find_youtube_id_in_element(tag: Tag) -> str | None:
    """tag自身、またはその内部（属性・iframe・aリンクのどれでもよい）からYouTube動画IDを探す。"""
    video_id = extract_youtube_id(tag.get("data-src"))
    if video_id:
        return video_id

    inner_iframe = tag.find("iframe", src=True)
    if inner_iframe:
        video_id = extract_youtube_id(inner_iframe["src"])
        if video_id:
            return video_id

    for a in tag.find_all("a", href=True):
        video_id = extract_youtube_id(a["href"])
        if video_id:
            return video_id

    return None


def make_youtube_iframe(soup: BeautifulSoup, video_id: str, source_id: str | None = None) -> Tag:
    iframe = soup.new_tag(
        "iframe",
        width="560",
        height="315",
        src=f"https://www.youtube.com/embed/{video_id}",
        frameborder="0",
        allowfullscreen=None,
    )
    # 置換元要素のidを引き継ぐ（paywall_element_idがこの要素を指している場合の対策）
    if source_id:
        iframe["id"] = source_id
    return iframe


def convert_youtube_embeds(soup: BeautifulSoup) -> int:
    """
    note編集画面でYouTube動画を「埋め込み」ではなく単なる「リンク」として
    貼った場合、note側のoEmbed解決に失敗して embedded-service="external-article"
    （汎用リンクカード）扱いになり、「- YouTube / Enjoy the videos...」という
    メタデータ文言つきの黒いエラーカードとして残ることがある（エラー153の一因）。
    embedded-service の値を問わず、YouTubeへのリンクを含む要素はすべて対象にする
    ことで、この種の埋め込み崩れも含めて一括で標準iframeに直す。
    """
    count = 0

    # 1. note が <figure> で包んだ埋め込み/リンクカードすべて（embedded-serviceの値は問わない）。
    #    figure内にYouTubeへの参照（data-src / 内部iframe / 内部aリンク）があれば、
    #    メタデータ文言ごとfigure全体を標準iframeに置き換える。
    for figure in soup.find_all("figure"):
        video_id = find_youtube_id_in_element(figure)
        if video_id:
            figure.replace_with(make_youtube_iframe(soup, video_id, figure.get("id")))
            count += 1

    # 2. figureに包まれていない、裸のYouTube iframeが残っていた場合の保険
    for iframe in soup.find_all("iframe", src=True):
        video_id = extract_youtube_id(iframe["src"])
        if not video_id:
            continue
        if iframe.get("width") == "560" and iframe.get("height") == "315" and not iframe.get("style"):
            continue  # 既に標準形式
        iframe.replace_with(make_youtube_iframe(soup, video_id, iframe.get("id")))
        count += 1

    # 3. figureにもiframeにも包まれていない、本文中の裸のYouTubeリンク（<a href>）
    for a in soup.find_all("a", href=True):
        video_id = extract_youtube_id(a["href"])
        if not video_id:
            continue
        a.replace_with(make_youtube_iframe(soup, video_id))
        count += 1

    return count


def convert_generic_link_cards(soup: BeautifulSoup) -> int:
    """
    YouTube/自記事へのリンク以外の「外部サイトへのリンクカード」
    （class="external-article-widget"：タイトル・説明・ドメインが枠で囲まれた
    プレビューカード。Twitcastingの入会案内など、note編集画面でURLをそのまま
    貼ったときに自動生成される）を、枠の無いシンプルなテキストリンクに変換する。
    YouTube変換・自記事リンク変換の後に実行するため、それらは対象に残らない。
    """
    count = 0
    for widget in soup.find_all("div", class_="external-article-widget"):
        link_el = widget.find("a", href=True)
        url = link_el["href"] if link_el else None
        if not url:
            continue

        title_el = widget.find(class_="external-article-widget-title")
        title = title_el.get_text(strip=True) if title_el else url

        p = soup.new_tag("p", **{"class": "external-link"})
        a = soup.new_tag("a", href=url, target="_blank", rel="noopener noreferrer")
        a.string = title
        p.append(a)

        figure = widget.find_parent("figure")
        target_el = figure if figure is not None else widget
        # 置換元要素のidを引き継ぐ（paywall_element_idがこの要素を指している場合の対策）
        if target_el.get("id"):
            p["id"] = target_el["id"]
        target_el.replace_with(p)
        count += 1

    return count


def strip_injected_header(soup: BeautifulSoup) -> None:
    """
    note_downloader.py が記事保存時に付与する、本文冒頭の
    <h1>タイトル</h1><p><a>出典URL</a></p><hr> を取り除く。
    タイトル・出典URLは既にメタデータ（<title>や<meta name="source-url">、
    build_master_index.py が出力するJSON/CSV）側に保持されているため本文には不要で、
    残したままだとWordPress側で投稿タイトルと本文冒頭のタイトルが二重表示になる。
    """
    body = soup.body
    if body is None:
        return

    h1 = body.find("h1", recursive=False)
    if h1 is None:
        return  # 想定外の構造なら何もしない（誤って本文を壊さないため）
    h1.decompose()

    citation_p = body.find("p", recursive=False)
    if citation_p is not None:
        links = citation_p.find_all("a")
        is_pure_citation_link = (
            len(links) == 1 and citation_p.get_text(strip=True) == links[0].get_text(strip=True)
        )
        if is_pure_citation_link:
            citation_p.decompose()

    hr = body.find("hr", recursive=False)
    if hr is not None:
        hr.decompose()


def is_note_toc_nav(tag) -> bool:
    """note.com側が描画した目次navかどうか（変換後の自前の<nav class="toc">は除く）。

    【2026-10-11追記：note側の目次DOM変更への対応】従来のnoteは
    <nav class="o-tableOfContents" data-v-...> という固定クラスで目次を描画していたが、
    2026-10頃のnote.comフロントエンド刷新で
    <div role="presentation"><nav aria-label="目次" class="mt-9"><details>...<ol>...
    「すべて表示」ボタン...</details></nav></div>
    というTailwindクラスのみの構造に変わり、o-tableOfContentsクラスが消えた。
    クラス名は今後も変わりうるため、aria-label="目次"（アクセシビリティ属性で
    比較的安定）でも判定する。
    """
    if getattr(tag, "name", None) != "nav":
        return False
    classes = tag.get("class") or []
    if "o-tableOfContents" in classes:
        return True
    return tag.get("aria-label") == "目次" and "toc" not in classes


def rebuild_toc(soup: BeautifulSoup) -> int:
    old_nav = soup.find(is_note_toc_nav)
    if old_nav is not None:
        # 新構造ではnavが<div role="presentation">に単独で包まれているため、
        # 包みごと置き換える（空のdivを残さない）。
        wrapper = old_nav.parent
        if (
            getattr(wrapper, "name", None) == "div"
            and wrapper.get("role") == "presentation"
            and len([c for c in wrapper.contents if getattr(c, "name", None) or str(c).strip()]) == 1
        ):
            old_nav = wrapper
    insert_after_tag = old_nav

    # 旧nav内の見出し（新構造では<summary>内に<h2>目次</h2>がある）はTOC項目に含めない
    headings = [
        h for h in soup.find_all(["h2", "h3"])
        if not (old_nav is not None and (h is old_nav or old_nav in h.parents))
    ]
    if not headings:
        if old_nav is not None:
            old_nav.decompose()
        return 0

    new_nav = soup.new_tag("nav", **{"class": "toc", "aria-label": "目次"})
    toc_list = soup.new_tag("ul", **{"class": "toc-list"})
    new_nav.append(toc_list)

    toc_entry_count = 0
    for i, h in enumerate(headings):
        text = h.get_text(strip=True)
        if not text:
            continue  # note編集画面の見た目調整用の空見出し（区切り）はTOCに載せない

        heading_id = h.get("id")
        if not heading_id:
            heading_id = f"toc-heading-{i}"
            h["id"] = heading_id

        li = soup.new_tag("li", **{"class": f"toc-item toc-{h.name}"})
        a = soup.new_tag("a", href=f"#{heading_id}")
        a.string = text
        li.append(a)
        toc_list.append(li)
        toc_entry_count += 1

    if toc_entry_count == 0:
        if old_nav is not None:
            old_nav.decompose()
        return 0

    if insert_after_tag is not None:
        insert_after_tag.replace_with(new_nav)
    else:
        # 元のnavが見つからない場合は本文の先頭に挿入
        body = soup.body
        if body is not None:
            body.insert(0, new_nav)

    return toc_entry_count


def convert_file(
    path: Path,
    key_to_filename: dict[str, str],
    key_to_title: dict[str, str],
) -> Tag:
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    strip_injected_header(soup)
    convert_internal_links(soup, key_to_filename)
    convert_note_embed_cards(soup, key_to_filename, key_to_title)
    convert_youtube_embeds(soup)
    convert_generic_link_cards(soup)
    rebuild_toc(soup)
    return soup


def main() -> None:
    args = parse_args()
    input_dir = Path(args.input_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"入力元: {input_dir}")
    print(f"出力先: {output_dir}")
    print()
    print("note記事ファイルを検索中...")
    articles = find_article_files(input_dir, output_dir)
    print(f"変換対象: {len(articles)} 件のnote記事HTMLを検出しました。")

    if not articles:
        print("変換対象のファイルが見つかりませんでした。")
        return

    key_to_filename = build_key_to_filename_map(articles)
    key_to_title = build_key_to_title_map(articles)

    total = len(articles)
    converted = 0
    skipped = 0
    failed: list[str] = []

    for i, (path, key, _title) in enumerate(articles, start=1):
        out_name = key_to_filename[key]
        out_path = output_dir / out_name

        if not args.force and out_path.exists():
            skipped += 1
            print(f"[{i}/{total}] スキップ（変換済み）: {out_name}")
            continue

        print(f"[{i}/{total}] 変換中: {out_name}")
        try:
            soup = convert_file(path, key_to_filename, key_to_title)
            out_path.write_text(str(soup), encoding="utf-8")
            converted += 1
        except Exception as e:
            failed.append(f"{path} : {e}")
            print(f"    -> [失敗] {e}")

    print()
    print("=" * 60)
    print(f"完了: 新規変換 {converted} 件 / スキップ {skipped} 件 / 失敗 {len(failed)} 件（全 {total} 件中）")
    if failed:
        print("失敗したファイル:")
        for f in failed:
            print(f"  - {f}")
    print("=" * 60)


if __name__ == "__main__":
    main()
