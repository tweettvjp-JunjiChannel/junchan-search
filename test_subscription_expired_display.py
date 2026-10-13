"""【2026-10-13】note-style-engagement.php のインラインJSを抽出し、模擬Codocウィジェットの各状態で
サブスク期限切れ表示・文言変更が意図通りに動くかをPlaywrightで検証する（本番非接触）。"""
import json
import os
import re
import sys
from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8")
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "note-style-engagement.php")
src = open(SRC, encoding="utf-8").read()
css = re.search(r'<style id="nseb-frontend-css">(.*?)</style>', src, re.S).group(1)
js = re.search(r'<script id="nseb-frontend-js">(.*?)</script>', src, re.S).group(1)
js = js.replace("<?php echo wp_json_encode($rest_root); ?>", '"http://test.local/wp-json/engage/v1"')
js = js.replace("<?php echo wp_json_encode(self::CONTENT_SUBSCRIPTION_DOM_ID); ?>", '"codoc-subscription-oEplngWcvQ"')
assert "<?php" not in js, "未置換のPHPタグがあります"

PAID = {"purchased_count": 0, "price_label": "¥300", "is_free": False, "view_count": 5,
        "like_count": 1, "view_today": 0, "view_week": 0, "view_month": 0}
PAID_100 = dict(PAID, price=100, price_label="100円")
FREE = dict(PAID, price=0, price_label="無料", is_free=True, purchased_count=None)
FREE_IDS = {"900"}
CHEAP_IDS = {"500"}


def codoc_block(logged_in, locked, offers_sub):
    user = '<div class="codoc-user"><strong>テスト読者</strong></div>' if logged_in else ""
    buy_style = "" if locked else ' style="display: none;"'
    sub = ('<ul class="codoc-subscription-list"><li class="codoc-subscription-group">'
           '<div class="codoc-subscription-info"><div class="codoc-subscription-title">月額読み放題</div></div></li></ul>'
           if offers_sub else "")
    body_after = "" if locked else "続きの本文です。" * 10
    pad = "x" * 600
    return (f'<div class="wp-block-codoc-codoc-block"><div class="codoc-entries">{user}'
            f'<div class="codoc-buy-wrap"{buy_style}><div class="codoc-buy">記事を購入</div>{sub}</div>'
            f'<div class="codoc-entry-body-after">{body_after}</div><span hidden>{pad}</span>'
            f'<div class="codoc-copyright">powered by codoc</div></div></div>')


def detail_page(post_id, block):
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{css}</style></head>
<body class="single postid-{post_id}"><h1 class="entry-title">テスト記事</h1>
<div class="nseb-status-bar" data-post-id="{post_id}" data-paid="1">
  <span class="nseb-stat nseb-stat-purchased nseb-skeleton" hidden style="display:none">…</span>
  <span class="nseb-stat nseb-price nseb-skeleton">…</span>
  <span class="nseb-stat nseb-stat-views"><span class="nseb-view-count nseb-skeleton">…</span></span>
  <button type="button" class="nseb-stat nseb-like-btn" data-post-id="{post_id}"><span class="nseb-heart">♡</span><span class="nseb-like-count">…</span></button>
</div>
<div class="entry-content"><p>無料部分</p>{block}</div>
<script>{js}</script></body></html>"""


def library_page(ids):
    cards = "".join(f'<article id="post-{i}"><div class="entry-card-meta"></div></article>' for i in ids)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{css}</style></head><body class="home">
<div id="nseb-my-library-tabs"><a href="#" data-tab="purchased" class="nseb-library-tab is-active">📖 サブスク既読記事</a>
<a href="#" data-tab="liked" class="nseb-library-tab">♥ スキした記事</a>
<a href="#" data-tab="both" class="nseb-library-tab">🌟 サブスク既読記事＆スキ</a></div>{cards}
<script>{js}</script></body></html>"""


results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("✅ 合格" if cond else "❌ 不合格"), name, detail)


# マイ本棚クラウド同期のサーバー（/engage/v1/library）モック。PCで保存済みの履歴を想定。
CLOUD = {"liked": [], "read": []}
LIBRARY_REQUESTS = []


def run(ctx, html, storage, codoc=None, path="/page", cloud=None):
    """codoc: None=Codoc未ログイン(トークン無し) / 'active'=購読中 / 'inactive'=ログイン中だが購読無効 /
    'invalid'=トークン失効。Codoc APIはモックで応答する（本番非接触）。"""
    page = ctx.new_page()
    CLOUD.update(cloud or {"liked": [], "read": []})

    def handler(route):
        url = route.request.url
        if "/card-data" in url:
            ids = re.search(r"ids=([0-9,]+)", url).group(1).split(",")
            route.fulfill(status=200, content_type="application/json", body=json.dumps({i: (FREE if i in FREE_IDS else PAID_100 if i in CHEAP_IDS else PAID) for i in ids}))
        elif "/engage/v1/library" in url:
            req = json.loads(route.request.post_data or "{}")
            LIBRARY_REQUESTS.append(req)
            if not req.get("token"):
                route.fulfill(status=401, content_type="application/json", body='{"ok":false}')
                return
            for k_req, k in (("liked", "liked"), ("read", "read")):
                CLOUD[k] = sorted(set(CLOUD[k]) | set(req.get(k_req, [])))
            for op in req.get("ops", []):
                key = "liked" if op["type"] in ("like", "unlike") else "read"
                if op["type"] in ("like", "read"):
                    CLOUD[key] = sorted(set(CLOUD[key]) | {op["id"]})
                else:
                    CLOUD[key] = [i for i in CLOUD[key] if i != op["id"]]
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, **CLOUD}))
        elif "/like/" in url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"like_count": 1}))
        elif "/view/" in url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(PAID))
        elif url.startswith("http://test.local/page") or url.startswith("http://test.local/?view=my-library"):
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
        else:
            route.fulfill(status=200, body="[]", content_type="application/json")

    def codoc_handler(route):
        url = route.request.url
        cors = {"Access-Control-Allow-Origin": "http://test.local", "Access-Control-Allow-Credentials": "true"}
        if "/users" in url:
            body = {"status": True, "user": {"code": "u1", "name": "テスト読者"}} if codoc in ("active", "inactive") else {"status": False}
        elif "/subscriptions/oEplngWcvQ" in url:
            body = {"status": True, "subscription": {"code": "oEplngWcvQ"}} if codoc == "active" else {"subscription": None, "status": False}
        else:
            body = {"status": False}
        route.fulfill(status=200, content_type="application/json", headers=cors, body=json.dumps(body))

    page.route("http://test.local/**", handler)
    page.route("https://codoc.jp/**", codoc_handler)
    page.goto("http://test.local/blank")
    st = dict(storage)
    if codoc:
        st["codocTokenCode"] = "tok-" + codoc
    page.evaluate("s => { localStorage.clear(); sessionStorage.clear(); for (const k in s) localStorage.setItem(k, s[k]); }", st)
    page.goto("http://test.local" + path)
    page.wait_for_timeout(1000)
    return page


def ls(page, key):
    return page.evaluate("k => localStorage.getItem(k)", key)


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    ctx = browser.new_context()

    # A: 新規ブラウザ（閲覧履歴なし）＋Codocログイン中・購読無効＋プラン対象記事でロック → 期限切れ
    p = run(ctx, detail_page(101, codoc_block(True, True, True)), {"nseb_purchased_posts": "[101]"}, codoc="inactive")
    stat = p.inner_text(".nseb-stat-purchased")
    check("A1 期限切れフラグが立つ", ls(p, "codoc_subscription_expired") == "1")
    check("A2 読み放題推定フラグは無い", ls(p, "codoc_active_subscriber") is None)
    check("A3 詳細ページに期限切れ案内枠が出る", p.locator(".nseb-sub-expired-notice").count() == 1)
    check("A4 ステータスバーが『サブスク期限切れ（要更新）』", "サブスク期限切れ" in stat, stat)
    check("A5 マイ本棚の既読記録は消えない", 101 in json.loads(ls(p, "nseb_purchased_posts")))
    check("A6 案内枠が統合案内枠より上にある",
          p.evaluate("() => { const n=document.querySelector('.nseb-sub-expired-notice'), g=document.querySelector('.nseb-purchase-guide'); return !!(n&&g&&(n.compareDocumentPosition(g)&Node.DOCUMENT_POSITION_FOLLOWING)); }"))
    check("A7 統合案内枠の復元ボタン文言", "購入済み・サブスクを復元" in p.inner_text(".nseb-purchase-guide-btn"))
    p.close()

    # B: 購読中アカウントでアンロック → 期限切れ表示なし・読み放題
    p = run(ctx, detail_page(101, codoc_block(True, False, True)), {"codoc_subscription_expired": "1"}, codoc="active")
    stat = p.inner_text(".nseb-stat-purchased")
    check("B1 購読中なら期限切れフラグ解除", ls(p, "codoc_subscription_expired") is None)
    check("B2 期限切れ案内枠は出ない", p.locator(".nseb-sub-expired-notice").count() == 0)
    check("B3 ステータスバーが読み放題", "読み放題" in stat, stat)
    check("B4 『購入済み』表記が残っていない", "購入済み" not in stat, stat)
    p.close()

    # C: 新規ブラウザ・履歴ゼロでも、Codocログイン中・購読無効なら詳細ページで期限切れ（端末非依存）
    p = run(ctx, detail_page(201, codoc_block(True, True, True)), {}, codoc="inactive")
    check("C1 履歴ゼロでも期限切れ判定", ls(p, "codoc_subscription_expired") == "1")
    check("C2 案内枠が出る", p.locator(".nseb-sub-expired-notice").count() == 1)
    p.close()

    # D: 購読中アカウントで、購読プラン未紐付け記事がロック → 期限切れ扱いしない
    p = run(ctx, detail_page(301, codoc_block(True, True, False)), {"nseb_purchased_posts": "[101,102,301]"}, codoc="active")
    check("D1 プラン未紐付け記事では期限切れにならない", ls(p, "codoc_subscription_expired") is None)
    check("D2 従来どおり誤った既読フラグは取り消される", 301 not in json.loads(ls(p, "nseb_purchased_posts")))
    p.close()

    # E: Codoc未ログイン（トークン無し）でロック → 期限切れにしない（古いフラグも消える）
    p = run(ctx, detail_page(101, codoc_block(False, True, True)), {"codoc_subscription_expired": "1"})
    check("E1 未ログインでは期限切れにならない", ls(p, "codoc_subscription_expired") is None)
    check("E2 未ログインでは案内枠なし", p.locator(".nseb-sub-expired-notice").count() == 0)
    p.close()

    # F: 旧不具合の回帰確認：ログイン済み(.codoc-user)だけでアンロック扱いにしない
    p = run(ctx, detail_page(401, codoc_block(True, True, False)), {}, codoc="active")
    check("F1 .codoc-userのみでは既読フラグを立てない", ls(p, "nseb_purchased_posts") in (None, "[]"))
    p.close()

    # G: 新規ブラウザ（履歴ゼロ・フラグゼロ）でトップ一覧/マイ本棚 → Codoc照会だけで全有料記事に要サブスク更新
    p = run(ctx, library_page([101, 102, 500, 900]), {}, codoc="inactive")
    p.wait_for_selector("#post-500 .nseb-card-purchased.is-expired", timeout=5000)
    check("G1 マイ本棚に期限切れ案内", p.locator(".nseb-library-expired-notice").count() == 1)
    for i, label in ((101, "300円"), (102, "300円"), (500, "100円")):
        loc = p.locator(f"#post-{i} .nseb-card-purchased")
        t = loc.inner_text() if loc.count() else ""
        check(f"G2 未読の{label}記事({i})に『要サブスク更新』", "要サブスク更新" in t, t)
    check("G3 無料記事(900)には期限切れバッジを出さない", p.locator("#post-900 .is-expired").count() == 0)
    p.close()

    # H: 購読中アカウント → 一覧は読み放題、案内なし
    p = run(ctx, library_page([101, 500]), {}, codoc="active")
    p.wait_for_selector("#post-500 .nseb-card-purchased.is-readfree", timeout=5000)
    check("H1 購読中は一覧が『読み放題』", "読み放題" in p.inner_text("#post-500 .nseb-card-purchased"))
    check("H2 購読中は案内なし", p.locator(".nseb-library-expired-notice").count() == 0)
    p.close()

    # I: 未ログイン・既読履歴あり → 『✅既読』、期限切れなし
    p = run(ctx, library_page([101]), {"nseb_purchased_posts": "[101]"})
    p.wait_for_selector("#post-101 .nseb-card-purchased")
    b = p.inner_text("#post-101 .nseb-card-purchased")
    check("I1 既読カードが『✅既読』", "既読" in b and "購入済み" not in b, b)
    check("I2 未ログインなら案内なし", p.locator(".nseb-library-expired-notice").count() == 0)
    p.close()

    # J: 古い期限切れフラグが残っていても、トークン失効（ログアウト扱い）なら表示を消す
    p = run(ctx, library_page([101, 500]), {"codoc_subscription_expired": "1"}, codoc="invalid")
    p.wait_for_timeout(500)
    check("J1 トークン失効で期限切れフラグ解除", ls(p, "codoc_subscription_expired") is None)
    check("J2 一覧に期限切れバッジが残らない", p.locator(".nseb-card-purchased.is-expired").count() == 0)
    check("J3 マイ本棚の案内も消える", p.locator(".nseb-library-expired-notice").count() == 0)
    p.close()

    # K: スマホ（新規ブラウザ・履歴ゼロ）でCodocログイン → PCで保存済みの既読/スキがマイ本棚に同期される
    LIBRARY_REQUESTS.clear()
    p = run(ctx, library_page([]), {}, codoc="active", path="/?view=my-library&tab=purchased",
            cloud={"liked": [101], "read": [102, 500]})
    p.wait_for_timeout(1500)
    check("K1 クラウドからスキ履歴を取得", json.loads(ls(p, "nseb_liked_posts") or "[]") == [101], ls(p, "nseb_liked_posts"))
    check("K2 クラウドから既読履歴を取得", sorted(json.loads(ls(p, "nseb_purchased_posts") or "[]")) == [102, 500], ls(p, "nseb_purchased_posts"))
    check("K3 マイ本棚が同期後のIDで開き直される", "purchased_ids" in p.url and "102" in p.url and "500" in p.url, p.url)
    check("K4 トークンをサーバーへ送っている", any(r.get("token") == "tok-active" for r in LIBRARY_REQUESTS))
    p.close()

    # L: ログイン中にスキを解除 → 『unlike』操作がサーバーへ送られ、和集合マージで復活しない
    LIBRARY_REQUESTS.clear()
    p = run(ctx, detail_page(101, codoc_block(True, False, True)), {"nseb_liked_posts": "[101]"}, codoc="active",
            cloud={"liked": [101], "read": []})
    p.click(".nseb-like-btn")
    p.wait_for_timeout(1200)
    ops = [op for r in LIBRARY_REQUESTS for op in r.get("ops", [])]
    check("L1 スキ解除がunlike操作として送信", {"type": "unlike", "id": 101} in ops, str(ops))
    check("L2 クラウド側からも101のスキが消える", 101 not in CLOUD["liked"], str(CLOUD["liked"]))
    p.close()

    # M: 未ログイン（トークン無し）ではサーバーへ一切送らない（従来どおり端末内のみ）
    LIBRARY_REQUESTS.clear()
    p = run(ctx, library_page([101]), {"nseb_purchased_posts": "[101]"})
    p.wait_for_timeout(800)
    check("M1 未ログインでは同期リクエストなし", len(LIBRARY_REQUESTS) == 0, str(len(LIBRARY_REQUESTS)))
    p.close()

    browser.close()

ng = [n for n, ok in results if not ok]
print(f"\n合計 {len(results)} 件中 {len(results) - len(ng)} 件合格")
sys.exit(1 if ng else 0)
