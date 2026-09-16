"""
「テーマ別3階層カテゴリー（大分類→小分類→タグ）」サイドバー用アコーディオン
HTML/JSを生成するスクリプト。

wp-admin側の「カスタムHTML」ウィジェット（このリポジトリには実体が無く、
wp-adminのウィジェット編集画面にのみ存在する。CLAUDE.md参照）に貼り付ける
静的HTML+JSを、WordPress REST APIから取得した実際のカテゴリー階層（大カテゴリー
=parent0、子カテゴリー=parentあり）とカテゴリーごとの投稿数、および
compute_category_tags.py が集計した「小分類ごとの主要タグ」を基に自動生成する。

Custom HTMLウィジェットはPHPを実行できない（静的HTML+JSのみ）ため、
「今どのページを見ているか」の判定は、埋め込んだJSがwindow.location.pathname
を見て、リンクのスラッグと突き合わせる方式で行う（サーバーサイドの状態は
一切使わない。ページ本体はWordPressの標準カテゴリー/タグアーカイブURL
（/category/{slug}/、/tag/{slug}/）そのものであり、そのURLのパス自体が
選択中カテゴリー/タグの状態そのものになるため、リロード・別ページからの
遷移でも自然に維持される）。

【2026-09-02追記：状態の完全永続化】アコーディオンの開閉状態（ユーザーが
手で開いた大分類・小分類）と、サイドバー自体のスクロール位置は、URLだけでは
再現できない「ユーザー操作の履歴」のため、sessionStorageで別途永続化する。
記事詳細ページへ実際に遷移してブラウザバックで戻る（＝フルページ再読み込みが
発生する）場合も含め、常に同じ開閉状態・スクロール位置を復元する。

実行方法:
    python generate_category_sidebar_widget.py            # HTML生成のみ
    python generate_category_sidebar_widget.py --push      # ウィジェットへ反映
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = SCRIPT_DIR / "wp_credentials.json"
CATEGORY_TAGS_PATH = SCRIPT_DIR / "category_tags.json"
OUTPUT_PATH = SCRIPT_DIR / "category_sidebar_widget.html"
# wp/v2/widgets REST APIで直接instance.raw.contentを書き換える（Playwright+
# CodeMirror操作は不要）。
WIDGET_ID = "custom_html-4"
WIDGET_TITLE = "記事カテゴリー"

# 9大分類×22小分類体系（categorize_articles_v2.py の TAXONOMY と完全に一致させる
# こと）。ユーザー指定の意味的な並び順（1〜9）をそのまま使う（投稿数順ではない）。
MAJOR_ORDER = [
    "思考・倫理・精神構造の解体",
    "国内政治・権力監視",
    "国際政治・グローバルガバナンス",
    "歴史の深掘り・真相究明",
    "医療・健康・生命倫理",
    "宗教・思想・社会構造",
    "科学技術・環境・AI",
    "文化・エンターテインメントの裏側",
    "その他",
]


def load_credentials() -> dict:
    return json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))


def load_category_tags() -> dict[str, list[dict]]:
    if not CATEGORY_TAGS_PATH.exists():
        return {}
    return json.loads(CATEGORY_TAGS_PATH.read_text(encoding="utf-8"))


def fetch_all_categories(site_url: str, auth: tuple[str, str]) -> list[dict]:
    cats = []
    page = 1
    s = requests.Session()
    s.auth = auth
    while True:
        r = s.get(
            f"{site_url}/wp-json/wp/v2/categories",
            params={"per_page": 100, "page": page, "_fields": "id,name,slug,parent,count"},
            timeout=30,
        )
        if r.status_code == 400:
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        cats.extend(batch)
        if page >= int(r.headers.get("X-WP-TotalPages", "1")):
            break
        page += 1
    return cats


def build_html(categories: list[dict], category_tags: dict[str, list[dict]]) -> str:
    by_name = {c["name"]: c for c in categories}
    children_by_parent: dict[int, list[dict]] = {}
    for c in categories:
        children_by_parent.setdefault(c["parent"], []).append(c)

    majors = [by_name[name] for name in MAJOR_ORDER if name in by_name]

    items_html = []
    for major in majors:
        # 小分類はユーザー指定の意味的な並び順を維持するため、投稿数ではなく
        # カテゴリー作成順（id昇順。categorize_articles_v2.py のTAXONOMY定義順と一致）で並べる。
        subs = sorted(children_by_parent.get(major["id"], []), key=lambda c: c["id"])
        # 【重要】WordPressの子カテゴリーの実際のアーカイブURLは
        # /category/{親スラッグ}/{子スラッグ}/ という入れ子構造になる
        # （子スラッグ単体の /category/{子スラッグ}/ ではない）。これを
        # 誤って子スラッグ単体でリンクすると、実際のページURL
        # （window.location.pathname）と一致せず、active判定・自動展開が
        # 一切機能しなくなる不具合になる（実機で発生・修正済み）。
        sub_items = []
        for s in subs:
            tags = category_tags.get(str(s["id"]), [])
            # 【2026-09-07追記：カテゴリ×タグ複合絞り込み（AND検索）】以前は
            # href="/tag/{slug}/" のみで、どの小分類配下から辿っても同じ
            # タグ全体（他カテゴリーの記事も含む）が表示されてしまい、
            # 「絞り込みの意味がない」というユーザーからの指摘（実例：
            # 「ワクチンカテゴリ下のトランプを押しても全カテゴリのトランプ
            # 記事が出てくる」）につながった。WordPress標準の公開クエリ変数
            # `cat`をタグアーカイブURLへ付加すると、WP_Queryが自動的に
            # カテゴリ×タグをAND結合したtax_queryを構築することを実機検証
            # 済み（該当カテゴリー×タグの交差件数と、実際に一覧・ページング
            # で取得できる件数が過不足なく一致することを確認）。念のため
            # custom-search-filter.php側にも明示的なpre_get_postsフックを
            # セーフティネットとして追加している（filter_tag_by_category）。
            tag_links = "".join(
                f'<li><a href="/tag/{html.escape(t["slug"])}/?cat={s["id"]}" data-cat-slug="/tag/{html.escape(t["slug"])}/?cat={s["id"]}" class="cat-acc-tag-link">{html.escape(t["name"])} <span class="cat-acc-count">({t["count"]})</span></a></li>'
                for t in tags
            )
            tag_block = f'<ul class="cat-acc-tag-list">{tag_links}</ul>' if tag_links else ""
            tag_toggle = '<button type="button" class="cat-acc-tag-toggle" aria-expanded="false" aria-label="関連タグを表示">🏷</button>' if tag_links else ""
            sub_items.append(
                f'<li class="cat-acc-sub" data-cat-slug="{html.escape(s["slug"])}">'
                f'<div class="cat-acc-sub-row">'
                f'<a href="/category/{html.escape(major["slug"])}/{html.escape(s["slug"])}/" data-cat-slug="{html.escape(s["slug"])}" class="cat-acc-sub-link">{html.escape(s["name"])} <span class="cat-acc-count">({s["count"]})</span></a>'
                f'{tag_toggle}'
                f'</div>'
                f'{tag_block}'
                f'</li>'
            )
        sub_items_html = "".join(sub_items)
        sub_block = f'<ul class="cat-acc-sublist">{sub_items_html}</ul>' if sub_items_html else ""
        toggle = '<button type="button" class="cat-acc-toggle" aria-expanded="false">▾</button>' if sub_items_html else '<span class="cat-acc-toggle-spacer"></span>'
        items_html.append(
            f'<li class="cat-acc-major" data-cat-slug="{html.escape(major["slug"])}">'
            f'<div class="cat-acc-major-row">'
            f'<a href="/category/{html.escape(major["slug"])}/" data-cat-slug="{html.escape(major["slug"])}" class="cat-acc-major-link">{html.escape(major["name"])} <span class="cat-acc-count">({major["count"]})</span></a>'
            f'{toggle}'
            f'</div>'
            f'{sub_block}'
            f'</li>'
        )

    list_html = "".join(items_html)

    return f"""<div class="cat-accordion-widget">
<style>
.cat-accordion-widget .cat-acc-tagindex-link{{display:block;margin:0 0 .8em;padding:.6em .5em;background:#f2f6fc;border-radius:6px;color:#3b7ddb;font-weight:bold;text-decoration:none;text-align:center;}}
.cat-accordion-widget .cat-acc-tagindex-link:hover{{background:#3b7ddb;color:#fff;}}
.cat-accordion-widget ul{{list-style:none;margin:0;padding:0;}}
.cat-accordion-widget .cat-acc-major-row{{display:flex;align-items:center;justify-content:space-between;}}
.cat-accordion-widget .cat-acc-major-link{{flex:1;padding:.5em .3em;text-decoration:none;color:#333;font-weight:bold;}}
.cat-accordion-widget .cat-acc-sub-row{{display:flex;align-items:center;justify-content:space-between;}}
.cat-accordion-widget .cat-acc-sub-link{{flex:1;display:block;padding:.4em .3em .4em 1.2em;text-decoration:none;color:#555;font-size:.92em;}}
.cat-accordion-widget .cat-acc-tag-link{{display:block;padding:.3em .3em .3em 2.1em;text-decoration:none;color:#777;font-size:.85em;}}
.cat-accordion-widget .cat-acc-count{{color:#999;font-size:.85em;font-weight:normal;}}
.cat-accordion-widget .cat-acc-toggle,.cat-accordion-widget .cat-acc-tag-toggle{{background:none;border:none;cursor:pointer;font-size:1em;padding:.3em .5em;color:#888;}}
.cat-accordion-widget .cat-acc-tag-toggle{{font-size:.85em;padding:.2em .5em;}}
.cat-accordion-widget .cat-acc-toggle-spacer{{display:inline-block;width:1.6em;}}
.cat-accordion-widget .cat-acc-sublist{{display:none;border-left:2px solid #eee;margin-left:.5em;}}
.cat-accordion-widget .cat-acc-tag-list{{display:none;border-left:2px dotted #eee;margin-left:1.6em;}}
.cat-accordion-widget li.cat-acc-open > .cat-acc-sublist{{display:block;}}
.cat-accordion-widget li.cat-acc-sub.cat-acc-open > .cat-acc-tag-list{{display:block;}}
.cat-accordion-widget li.cat-acc-open > .cat-acc-major-row .cat-acc-toggle{{transform:rotate(180deg);}}
.cat-accordion-widget li.cat-acc-sub.cat-acc-open > .cat-acc-sub-row .cat-acc-tag-toggle{{color:#3b7ddb;}}
.cat-accordion-widget a.cat-acc-candidate{{color:#3b7ddb;font-weight:bold;background:#eaf1fb;border-radius:4px;}}
.cat-accordion-widget a.cat-acc-active{{color:#d32f2f;font-weight:bold;background:#fdecea;border-radius:4px;}}
/* 【2026-09-17追記：「記事カテゴリー」見出しへの×リセットボタン新設】
   ウィジェットタイトル（テーマ側がh3.widget-titleとして出力、このスクリプト
   の外側にある）の右端に配置する。見出し自体をflexにして右寄せする。 */
#custom_html-4 h3.widget-title{{display:flex;align-items:center;justify-content:space-between;}}
.cat-acc-reset-btn{{background:none;border:none;font-size:1.1em;line-height:1;color:#999;cursor:pointer;padding:0 .2em;font-weight:normal;}}
.cat-acc-reset-btn:hover{{color:#333;}}
</style>
<a href="/tags/" class="cat-acc-tagindex-link">🔤 キーワードから探す（50音順）</a>
<ul class="cat-acc-list">{list_html}</ul>
<script>
(function () {{
  var root = document.currentScript.previousElementSibling;
  if (!root || !root.classList.contains('cat-acc-list')) {{
    root = document.querySelector('.cat-accordion-widget .cat-acc-list');
  }}
  if (!root) return;

  // 【2026-09-02追記：開閉状態の永続化】ユーザーが手で開いた大分類・小分類
  // （タグ一覧）を、そのdata-cat-slugの集合としてsessionStorageへ保存する。
  // 記事詳細へ実遷移してブラウザバックで戻る等のフルページ再読み込みでも、
  // このリストを読み直して同じ要素を開いた状態に復元する（URLからは
  // 導出できない「ユーザーが手で開いた」という操作履歴そのものを保存する）。
  var OPEN_NODES_KEY = 'nseb_open_nodes';
  function getOpenNodes() {{
    try {{
      var raw = sessionStorage.getItem(OPEN_NODES_KEY);
      var arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr : [];
    }} catch (e) {{ return []; }}
  }}
  function saveOpenNodes(arr) {{
    try {{ sessionStorage.setItem(OPEN_NODES_KEY, JSON.stringify(arr)); }} catch (e) {{}}
  }}
  function setNodeOpen(slug, open) {{
    var set = getOpenNodes();
    var idx = set.indexOf(slug);
    if (open && idx === -1) {{ set.push(slug); }}
    else if (!open && idx !== -1) {{ set.splice(idx, 1); }}
    saveOpenNodes(set);
  }}

  // 【2026-09-04追記：旧アコーディオン自動クローズ排他制御】タグ選択等で
  // 「一致判定によって自動展開された」ノードは、ユーザーが手でトグルボタンを
  // 押して開いた（OPEN_NODES_KEY）ものとは別枠で管理する。同じタグ・カテゴリー
  // を見ている間は開いたままにし、別のタグ・カテゴリーへ選択が変わった瞬間に
  // （ユーザーが手動で開いたものを除いて）自動的に閉じる。
  var CANDIDATE_OPEN_KEY = 'nseb_candidate_open_nodes';
  function getCandidateOpen() {{
    try {{
      var raw = sessionStorage.getItem(CANDIDATE_OPEN_KEY);
      var arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr : [];
    }} catch (e) {{ return []; }}
  }}
  function saveCandidateOpen(arr) {{
    try {{ sessionStorage.setItem(CANDIDATE_OPEN_KEY, JSON.stringify(arr)); }} catch (e) {{}}
  }}
  function closeNodeBySlug(slug) {{
    var li = root.querySelector('.cat-acc-major[data-cat-slug="' + CSS.escape(slug) + '"], .cat-acc-sub[data-cat-slug="' + CSS.escape(slug) + '"]');
    if (!li) return;
    li.classList.remove('cat-acc-open');
    var t = li.querySelector(':scope > .cat-acc-major-row .cat-acc-toggle, :scope > .cat-acc-sub-row .cat-acc-tag-toggle');
    if (t) {{ t.setAttribute('aria-expanded', 'false'); }}
  }}
  function closeStaleCandidates(newKeys) {{
    var prev = getCandidateOpen();
    var manual = getOpenNodes();
    prev.forEach(function (slug) {{
      if (newKeys.indexOf(slug) !== -1) return; // 引き続き該当中 → 閉じない
      if (manual.indexOf(slug) !== -1) return; // ユーザーが手動固定 → 閉じない
      closeNodeBySlug(slug);
    }});
  }}

  // 【2026-09-02追記：「マイ本棚」クリック時のリセット】マイ本棚は大分類のみの
  // 初期状態に戻す仕様のため、この関数をグローバル公開し、custom_html-3
  // ウィジェット側のクリックハンドラから呼び出せるようにする。
  window.nsebResetSidebarAccordion = function () {{
    saveOpenNodes([]);
    saveCandidateOpen([]);
    root.querySelectorAll('.cat-acc-open').forEach(function (li) {{
      li.classList.remove('cat-acc-open');
      var t = li.querySelector(':scope > .cat-acc-major-row .cat-acc-toggle, :scope > .cat-acc-sub-row .cat-acc-tag-toggle');
      if (t) {{ t.setAttribute('aria-expanded', 'false'); }}
    }});
  }};

  function openNode(li) {{
    if (!li) return;
    li.classList.add('cat-acc-open');
    var toggle = li.querySelector(':scope > .cat-acc-major-row .cat-acc-toggle, :scope > .cat-acc-sub-row .cat-acc-tag-toggle');
    if (toggle) {{ toggle.setAttribute('aria-expanded', 'true'); }}
  }}

  // 保存済みの開閉状態を復元する（ページ読み込み・PJAX差し替えのいずれでも
  // サイドバー自体は再生成されないため、通常は初回ロード時に1度呼べば足りる）。
  function restoreOpenNodes() {{
    var open = getOpenNodes();
    open.forEach(function (slug) {{
      var li = root.querySelector('.cat-acc-major[data-cat-slug="' + CSS.escape(slug) + '"], .cat-acc-sub[data-cat-slug="' + CSS.escape(slug) + '"]');
      if (li) {{ openNode(li); }}
    }});
  }}

  root.addEventListener('click', function (e) {{
    var btn = e.target.closest('.cat-acc-toggle, .cat-acc-tag-toggle');
    if (btn) {{
      var li = btn.closest('.cat-acc-major, .cat-acc-sub');
      if (!li) return;
      var open = li.classList.toggle('cat-acc-open');
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      setNodeOpen(li.getAttribute('data-cat-slug'), open);
      return;
    }}
  }});

  // 【2026-09-02追記：PJAX対応】現在のURLパスに一致するカテゴリー/タグリンクを
  // 探し、ハイライト＋その祖先（大分類・小分類）を展開する。初回ロード時だけで
  // なく、PJAX（custom-search-filter.phpのfetchベース部分更新）による一覧側
  // だけの遷移後にも呼び直せるよう、グローバル関数として公開する（サイドバー
  // 自体のDOM・スクロール位置・他の展開状態には一切触れず、リンクのactive
  // クラスと遷移先の展開状態だけをピンポイントで更新する）。
  // 以前ハイライトされていたリンクのactiveクラスは、新しい遷移先が確定した
  // 時点で明示的に外す（消し忘れると別カテゴリーへ移動した後も前の選択が
  // 青く残り続けるため）。展開状態（cat-acc-open）は閉じない＝ユーザーが
  // 手で開閉した状態を尊重し、新しい選択先だけを追加で開く。
  // 【2026-09-03追記：記事詳細ページ閲覧中も選択中コンテキストを維持】
  // カテゴリー/タグ/年月アーカイブのどれかを選んで記事一覧を見た後、記事を
  // 開くと現在のURLはどの一覧リンクとも一致しなくなる（当然、記事詳細URLは
  // カテゴリーURLではないため）。これまではその時点で無条件にactiveクラスを
  // 消していたため、記事詳細を読んでいる間サイドバーの赤色ハイライトが消えて
  // 「何を選んで辿り着いたか」が分からなくなる不具合が報告された。
  // 対策として、選択中パスを 'nseb_active_path' という共通キーでsessionStorage
  // に永続化し（アーカイブウィジェット側とも共有する）、現在のURLの扱いを
  // 3パターンに分岐させる：
  //   ① 現在のURLがこのウィジェット内のいずれかのリンクと一致する
  //      → それを新しい選択として保存・ハイライトする。
  //   ② 現在のURLがトップページ（/）そのもの
  //      → 明示的に「全体表示」に戻ったとみなし、選択状態をクリアする
  //        （📰記事ホームボタンの遷移先と同じ扱い）。
  //   ③ それ以外（記事詳細ページ等、どの一覧ページでもないURL）
  //      → 現在のURLからは何も判定せず、保存済みの選択状態をそのまま
  //        読み出してハイライトを復元する（＝何もクリアしない）。
  var ACTIVE_PATH_KEY = 'nseb_active_path';
  // 【2026-09-07追記：URL自己記述化によるnodeKey機構の撤廃】タグリンクが
  // /tag/{{slug}}/?cat={{sub_id}} という自己記述的なURLになったことで、
  // 「どの小分類配下のタグがクリックされたか」はURL自体（cat=の値）から
  // 100%特定できるようになった。以前の「クリックした瞬間に親スラッグを
  // sessionStorageへ記録しておく」nodeKey方式（ACTIVE_TAG_NODE_KEY）は
  // 不要になったため撤廃した。ページ番号が変わっても（/tag/{{slug}}/page/2/
  // ?cat=...）このURL自身の情報だけで赤色対象を一意に特定できる。
  function parseUrl(raw) {{
    var u;
    try {{ u = new URL(raw, window.location.href); }} catch (e) {{ u = null; }}
    var pathname = u ? u.pathname : String(raw).split('?')[0];
    // 【2026-09-13追記：percent-encoding大文字小文字ゆれ対策】ウィジェット内の
    // タグ/カテゴリーリンクは小文字percent-encoding（例: %e5%a4%a7、WordPress
    // の実際のスラッグ・get_term_link()の値と一致させたもの）で埋め込んでいる
    // が、日本語を含むURLへ生のUnicode文字経由（アドレスバーへの直接入力・
    // 貼り付け等）で遷移した場合、ブラウザ自身がそのURLをエンコードし直す
    // 際に大文字percent-encoding（%E5%A4%A7）になるケースが実機で確認された。
    // 単純な文字列比較（===）ではこの大文字小文字の違いだけで一致しなくなり、
    // タグ/カテゴリーページ遷移後にアコーディオン展開・青色ハイライトが
    // 効かなくなる不具合を引き起こしていた。decodeURIComponent()で実際の
    // 文字列（日本語そのもの）まで復元してから比較することで、encodingの
    // 大文字小文字ゆれを吸収する（本来比較したいのは文字列の中身であって
    // エンコード表現ではないため）。
    try {{ pathname = decodeURIComponent(pathname); }} catch (e) {{}}
    var path = pathname.replace(/\\/page\\/\\d+\\/?$/, '/').replace(/\\/$/, '') + '/';
    var cat = u ? u.searchParams.get('cat') : null;
    return {{ path: path, cat: cat }};
  }}
  // アーカイブウィジェット等、パスのみで比較する既存箇所向けの互換ヘルパー。
  function normalizePath(p) {{
    return parseUrl(p).path;
  }}
  // 【2026-09-06追記：仕様再徹底】前回（2026-09-05）導入した「サイドバー内
  // クリックは構造を一切変更しない」方式は撤回した。同じタグを含む他の
  // カテゴリーが青色展開される機能はユーザーの本来の要望であり、消しては
  // ならない。展開ロジックは常にフル実行し、その結果アクティブ（赤）要素が
  // 視界から外れてしまう問題は、展開後にkeepActiveInView()でサイドバーの
  // スクロール位置だけを補正して解決する（詳細はkeepActiveInViewの
  // コメント参照）。これにより、①他カテゴリーの青色展開を維持し、
  // ②ページ送り（.tc-pager、サイドバー外からのPJAX遷移）で新たに他の
  // カテゴリーが展開されて視点がズレても、赤色アクティブ項目が必ず視界内に
  // 収まるよう自動補正される。
  // 【2026-09-07追記：カテゴリ×タグURL自己記述化に伴う判定の作り直し】
  // タグリンクのhrefが /tag/{{slug}}/?cat={{sub_id}} という自己記述的な形式に
  // なったことで、同じタグでも小分類ごとにhrefが異なる（一致が重複しない）
  // ようになった。そのため「他カテゴリーの青色展開」機能を維持するには、
  // href完全一致ではなく「タグ部分（クエリを除いたpath）が同じかどうか」で
  // 候補集合を作り、その中で「cat=の値が現在のURLと一致するもの」だけを
  // 赤（active）、残りを青（candidate）にする、という判定に作り直す。
  // カテゴリー本体（大分類・小分類）のリンクはクエリを持たないため、
  // 従来通りpath完全一致で1件だけヒットする。
  function clearHighlightClasses() {{
    root.querySelectorAll('a.cat-acc-active, a.cat-acc-candidate').forEach(function (a) {{
      a.classList.remove('cat-acc-active');
      a.classList.remove('cat-acc-candidate');
    }});
  }}

  function highlightPath(path) {{
    clearHighlightClasses();

    var current = parseUrl(path);

    var catEls = [];
    root.querySelectorAll('.cat-acc-major-link, .cat-acc-sub-link').forEach(function (a) {{
      if (parseUrl(a.getAttribute('href')).path === current.path) {{ catEls.push(a); }}
    }});

    var tagMatches = [];
    root.querySelectorAll('a.cat-acc-tag-link').forEach(function (a) {{
      var p = parseUrl(a.getAttribute('href'));
      if (p.path === current.path) {{ tagMatches.push({{ el: a, cat: p.cat }}); }}
    }});

    if (!catEls.length && !tagMatches.length) return false;

    catEls.forEach(function (a) {{ a.classList.add('cat-acc-active'); }});

    var chosenTagEl = null;
    if (tagMatches.length && current.cat) {{
      tagMatches.forEach(function (m) {{ if (m.cat === current.cat) {{ chosenTagEl = m.el; }} }});
    }}
    tagMatches.forEach(function (m) {{
      m.el.classList.add(m.el === chosenTagEl ? 'cat-acc-active' : 'cat-acc-candidate');
    }});

    var newCandidateKeys = [];
    catEls.concat(tagMatches.map(function (m) {{ return m.el; }})).forEach(function (a) {{
      // タグリンク→小分類→大分類 の順に祖先をすべて展開対象として集める。
      var node = a.closest('.cat-acc-sub, .cat-acc-major');
      while (node) {{
        var key = node.getAttribute('data-cat-slug');
        if (key && newCandidateKeys.indexOf(key) === -1) {{ newCandidateKeys.push(key); }}
        var parentEl = node.parentElement ? node.parentElement.closest('.cat-acc-sub, .cat-acc-major') : null;
        node = parentEl;
      }}
    }});

    // 【2026-09-04追記：旧アコーディオン自動クローズ排他制御】新しい一致集合に
    // 含まれなくなった、以前の自動展開ノード（手動固定を除く）を閉じてから、
    // 新しい一致先を展開する。
    closeStaleCandidates(newCandidateKeys);
    newCandidateKeys.forEach(function (slug) {{
      var li = root.querySelector('.cat-acc-major[data-cat-slug="' + CSS.escape(slug) + '"], .cat-acc-sub[data-cat-slug="' + CSS.escape(slug) + '"]');
      if (li) {{ openNode(li); }}
    }});
    saveCandidateOpen(newCandidateKeys);

    // 【2026-09-06追記】展開・排他クローズの結果、赤色アクティブ要素が
    // サイドバーの視界外へ押し出されていないか確認し、外れていれば
    // その場でスクロール位置だけを補正する（DOM構造には一切触れない）。
    keepActiveInView();

    return true;
  }}

  // 【2026-09-13追記：検索結果ページ連動】検索窓から検索した際
  // （URLに ?s=キーワード が付く、custom-search-filter.php の検索結果ページ）は
  // highlightPath()が前提とする「/category/」「/tag/」パスに遷移しないため、
  // 従来の仕組みは一切反応しなかった。検索結果ページでもカテゴリー/タグ名と
  // キーワードが一致すれば自動展開・青色ハイライトされるよう、URLのパスでは
  // なく「カテゴリー/タグの表示名」とキーワードのテキスト一致で判定する
  // 経路を別途追加する。
  function extractLabelText(a) {{
    var text = (a.textContent || '');
    // 件数表記「 (123)」を末尾から除去してから比較する。
    text = text.replace(/\\s*\\([0-9,]+\\)\\s*$/, '');
    return text.trim().toLowerCase();
  }}

  function highlightKeyword(rawKeyword) {{
    clearHighlightClasses();

    // URLSearchParams.get()の時点でパーセントエンコーディング・'+'（半角スペース）
    // のデコードは完了済みのため、ここでは前後空白の除去と大文字小文字の統一のみ行う。
    var keyword = String(rawKeyword || '').trim().toLowerCase();
    if (!keyword) return false;

    var matches = [];
    root.querySelectorAll('.cat-acc-major-link, .cat-acc-sub-link, a.cat-acc-tag-link').forEach(function (a) {{
      var label = extractLabelText(a);
      if (!label) return;
      if (label.indexOf(keyword) !== -1 || keyword.indexOf(label) !== -1) {{
        matches.push(a);
      }}
    }});

    if (!matches.length) return false;

    matches.forEach(function (a) {{ a.classList.add('cat-acc-candidate'); }});

    var newCandidateKeys = [];
    matches.forEach(function (a) {{
      var node = a.closest('.cat-acc-sub, .cat-acc-major');
      while (node) {{
        var key = node.getAttribute('data-cat-slug');
        if (key && newCandidateKeys.indexOf(key) === -1) {{ newCandidateKeys.push(key); }}
        var parentEl = node.parentElement ? node.parentElement.closest('.cat-acc-sub, .cat-acc-major') : null;
        node = parentEl;
      }}
    }});

    closeStaleCandidates(newCandidateKeys);
    newCandidateKeys.forEach(function (slug) {{
      var li = root.querySelector('.cat-acc-major[data-cat-slug="' + CSS.escape(slug) + '"], .cat-acc-sub[data-cat-slug="' + CSS.escape(slug) + '"]');
      if (li) {{ openNode(li); }}
    }});
    saveCandidateOpen(newCandidateKeys);

    return true;
  }}

  // 現在のURL（または保存済みの選択パス）に対し、まず検索キーワード一致
  // （?s=）を優先的に試し、ヒットしなければ従来のカテゴリー/タグURL一致
  // （highlightPath）にフォールバックする。
  function highlightForUrl(rawPath) {{
    var keyword = null;
    try {{ keyword = new URL(rawPath, window.location.href).searchParams.get('s'); }} catch (e) {{}}
    if (keyword && keyword.trim() && highlightKeyword(keyword)) {{ return true; }}
    return highlightPath(rawPath);
  }}

  // 展開によってアクティブ（赤）要素がサイドバーの可視領域から外れた場合に
  // 限り、その要素が中央付近に収まるようスクロール位置を補正する。既に
  // 全体が見えている場合は何もしない（クリック直後、ユーザー自身が今まさに
  // 見ていた項目の位置を不要に動かさないため）。
  // 【2026-09-09追記：document全体から検索するよう変更】以前は`root`
  // （このウィジェット＝カテゴリー/タグのcat-acc-list）内だけを検索していた
  // ため、年代別アーカイブウィジェット（generate_archive_widget.py、別の
  // <script>スコープ）側のアクティブ要素はこの関数の対象外だった。
  // 記事詳細ページで「アーカイブ月」を選択した文脈から来た場合にも同じ
  // 保証を効かせるため、`.sidebar`配下のa.cat-acc-active全体（どちらの
  // ウィジェットのものでも）を対象にするよう変更した。
  function keepActiveInView() {{
    var sidebar = document.querySelector('.sidebar');
    if (!sidebar) return;
    var activeEl = sidebar.querySelector('a.cat-acc-active');
    if (!activeEl) return;
    var elRect = activeEl.getBoundingClientRect();
    var sbRect = sidebar.getBoundingClientRect();
    var elTopInSidebar = elRect.top - sbRect.top + sidebar.scrollTop;
    var elBottomInSidebar = elTopInSidebar + elRect.height;
    var visibleTop = sidebar.scrollTop;
    var visibleBottom = sidebar.scrollTop + sidebar.clientHeight;
    if (elTopInSidebar >= visibleTop && elBottomInSidebar <= visibleBottom) {{
      return; // 既に完全に視界内 → 何もしない。
    }}
    var target = elTopInSidebar - (sidebar.clientHeight / 2) + (elRect.height / 2);
    sidebar.scrollTop = Math.max(0, target);
  }}
  // 【2026-09-09追記：custom-search-filter.php側のpersistSidebarScroll()と
  // 連携するためグローバル公開】このウィジェットの<script>はサイドバーHTML内
  // （bodyパース中）で早期に実行されるため、フッターで読み込まれる
  // custom-search-filter.phpのスクリプトより必ず先に完了する。そのフッター
  // 側のpersistSidebarScroll()が「保存済みの生のスクロールpx値」で
  // scrollTopを上書きする処理を持っており、これが本関数の直後に実行される
  // ことで、せっかく計算したアクティブ要素の可視位置を再び覆い隠してしまう
  // 競合（記事詳細ページで赤色タグが枠外に隠れる不具合の真因）が実機で
  // 確認された。対策として、この関数をグローバル公開し、
  // persistSidebarScroll()側が生スクロール値の復元を行った「後に」再度
  // これを呼び直すことで、最終的な決定権を必ずこちら（アクティブ要素の
  // 可視性）に持たせる。
  window.nsebKeepActiveInView = keepActiveInView;

  function updateActiveState(pathOverride) {{
    // 【2026-09-07追記】cat=クエリを判定に使うため、pathnameだけでなく
    // クエリ文字列（search）も含めた完全なURLをhighlightPathへ渡す。
    // 【2026-09-13追記】検索結果ページ（?s=）はhighlightForUrl内で
    // highlightKeywordを優先的に試し、ヒットしなければ従来のhighlightPath
    // （/category/、/tag/パス一致）にフォールバックする。
    var rawPath = pathOverride || (window.location.pathname + window.location.search);

    if (highlightForUrl(rawPath)) {{
      try {{ sessionStorage.setItem(ACTIVE_PATH_KEY, rawPath); }} catch (e) {{}}
      return;
    }}
    if (parseUrl(rawPath).path === '/') {{
      try {{ sessionStorage.removeItem(ACTIVE_PATH_KEY); }} catch (e) {{}}
      // トップページ＝全体表示に戻った以上、以前タグ選択等で自動展開されていた
      // ノード（手動固定を除く）は排他的に閉じる。
      closeStaleCandidates([]);
      saveCandidateOpen([]);
      return;
    }}
    // 記事詳細ページ等、このウィジェットのどのリンクとも一致しないURL。
    // 直前に保存された選択状態があれば、それをそのまま復元表示する
    // （このウィジェット内のリンクに該当するものが無ければ何も光らない）。
    var saved;
    try {{ saved = sessionStorage.getItem(ACTIVE_PATH_KEY); }} catch (e) {{ saved = null; }}
    if (saved) {{ highlightForUrl(saved); }}
  }}

  restoreOpenNodes();
  updateActiveState();
  window.nsebUpdateSidebarActiveState = updateActiveState;
  // 【2026-09-02追記】「マイ本棚」クリック時、選択中コンテキストも明示的に
  // クリアする（アコーディオンの開閉リセットとあわせて、赤色ハイライトも
  // 一緒に消す）。
  var _origReset = window.nsebResetSidebarAccordion;
  window.nsebResetSidebarAccordion = function () {{
    if (_origReset) {{ _origReset(); }}
    try {{ sessionStorage.removeItem(ACTIVE_PATH_KEY); }} catch (e) {{}}
    root.querySelectorAll('a.cat-acc-active, a.cat-acc-candidate').forEach(function (a) {{
      a.classList.remove('cat-acc-active');
      a.classList.remove('cat-acc-candidate');
    }});
  }};

  // 【2026-09-17追記：「記事カテゴリー」見出しへの×リセットボタン新設】
  // ウィジェットタイトル（テーマのwidget wrapperが出力するh3.widget-title、
  // このスクリプトの外側の要素）の右端にボタンを挿入する。クリックで
  // 既存のnsebResetSidebarAccordion()（アコーディオン全閉じ＋赤/青
  // ハイライト解除、マイ本棚ボタンと共通のリセット処理）をそのまま呼ぶ。
  function insertCategoryResetButton() {{
    if (document.querySelector('.cat-acc-reset-btn')) {{ return; }} // 二重挿入防止
    var widgetRoot = root.closest('aside, .widget');
    var heading = widgetRoot ? widgetRoot.querySelector('h3.widget-title, h2.widget-title, .widgettitle') : null;
    if (!heading) {{ return; }}
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cat-acc-reset-btn';
    btn.setAttribute('aria-label', 'カテゴリー選択をリセット');
    btn.textContent = '×';
    btn.addEventListener('click', function (e) {{
      e.preventDefault();
      e.stopPropagation();
      if (typeof window.nsebResetSidebarAccordion === 'function') {{
        window.nsebResetSidebarAccordion();
      }}
    }});
    heading.appendChild(btn);
  }}
  insertCategoryResetButton();
}})();
</script>
</div>"""


def push_widget_content(site_url: str, auth: tuple[str, str], content: str) -> None:
    s = requests.Session()
    s.auth = auth
    r = s.post(
        f"{site_url}/wp-json/wp/v2/widgets/{WIDGET_ID}",
        json={"instance": {"raw": {"title": WIDGET_TITLE, "content": content}}},
        timeout=30,
    )
    r.raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--push", action="store_true", help="ウィジェットへ直接反映する（wp/v2/widgets REST API）")
    args = parser.parse_args()

    creds = load_credentials()
    site_url = creds["site_url"].rstrip("/")
    auth = (creds["username"], creds["application_password"])
    categories = fetch_all_categories(site_url, auth)
    print(f"取得カテゴリー数: {len(categories)}")
    category_tags = load_category_tags()
    print(f"タグデータ: {len(category_tags)}小分類分")

    widget_html = build_html(categories, category_tags)
    OUTPUT_PATH.write_text(widget_html, encoding="utf-8")
    print(f"生成しました: {OUTPUT_PATH} ({len(widget_html)}文字)")

    if args.push:
        push_widget_content(site_url, auth, widget_html)
        print(f"ウィジェット（{WIDGET_ID}）へ反映しました。")


if __name__ == "__main__":
    main()
