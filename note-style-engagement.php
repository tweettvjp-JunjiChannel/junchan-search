<?php
/**
 * Plugin Name: Note Style Engagement Bar
 * Description: 記事タイトル直下にnote風ステータスバー（価格・PV・スキ・購入数）を表示し、記事内の赤い案内枠にサブスクリプション登録ボタンを追加する。
 * Version: 3.1.1
 * Author: junchan-world
 */

if (!defined('ABSPATH')) {
    exit;
}

class Note_Style_Engagement_Bar {

    // 'like_count' は note.com インポート時に既に使われている別のメタキー
    // （note本家側のスキ数）と衝突するため、専用の名前空間付きキーを使う。
    const LIKE_META_KEY = 'nseb_like_count';
    const SIDEBAR_SUBSCRIPTION_WIDGET_ID = 'custom_html-2';
    const CONTENT_SUBSCRIPTION_DOM_ID = 'codoc-subscription-oEplngWcvQ';

    // Codoc価格・購入数のキャッシュ用postmeta（WP-Cronで定期更新。
    // 記事一覧ページ表示時に外部APIへライブアクセスして重くならないよう、
    // ページ表示時は常にこのキャッシュ済みpostmetaのみを読む）
    const CODOC_CACHE_PRICE_KEY = 'codoc_cached_price';
    const CODOC_CACHE_PURCHASED_KEY = 'codoc_cached_purchased_count';
    const CODOC_CACHE_UPDATED_KEY = 'codoc_cache_updated_at';
    // auto_sync_blogs.py の sync_codoc_discount が90日経過記事の価格を書き換える
    // "直前"に、値下げ前の元価格を保全するために書き込む専用メタキー。
    // このメタが無い（＝一度も値下げされていない）記事では、アーカイブ割引ボックスは
    // 「元がいくらだったか」を断定できないため、現在価格のみを案内する
    // （[[section 6のprice source of truthルール]]と同様、実測できない数字を捏造しない）。
    const PRICE_BEFORE_DISCOUNT_KEY = 'codoc_price_before_discount';
    // note記事に付与されるカテゴリー（custom-search-filter.php の CAT_MAP['note'] と同一ID）。
    const NOTE_CATEGORY_ID = 2471;
    // アーカイブ割引ボックスの対象とする経過日数（auto_sync_blogs.py の
    // CODOC_DISCOUNT_AFTER_DAYS と揃える。値下げ処理の対象記事＝アピール対象記事）。
    const ARCHIVE_DISCOUNT_AFTER_DAYS = 90;
    const CRON_HOOK = 'nseb_refresh_codoc_cache';
    // 単一記事ページを開くたびのライブ更新（handle_view参照）で、直前に
    // 更新されたばかりのキャッシュを毎回律儀に叩き直さないための最小間隔。
    // 短時間に同じ記事へアクセスが集中してもCodoc側へのリクエストが
    // 連打されないようにするレート制限であると同時に、E2Eテストが
    // codoc_cache_updated_at を「今」にセットしてpostmetaを直接注入した際に
    // ライブ更新で即座に上書きされてしまわないようにする効果も兼ねる。
    const LIVE_REFRESH_MIN_INTERVAL_SECONDS = 300;

    public function __construct() {
        add_action('init', array($this, 'register_meta'));
        add_action('rest_api_init', array($this, 'register_routes'));

        // 単一記事ページのタイトル（entry-title, h1）直後にステータスバーの
        // "骨組み"（スケルトン）だけを追記する。実際の数値（PV・スキ数・価格）は
        // ページ表示のたびにPHP側で焼き込まない。一覧ページ（card-data経由）と
        // 完全に同じREST APIを使ってブラウザ側で非同期に取得・描画することで、
        // 「一覧は最新、詳細はページキャッシュ由来の古い数字」という不整合の
        // 発生源を構造的に無くす（アーキテクチャ改修 2026-08-10）。
        add_filter('the_title', array($this, 'route_title_filter'), 10, 2);

        // 単一記事ページ・一覧ページ（トップ・アーカイブ・カテゴリー・検索結果等）
        // 共通のフロントエンドJS/CSSを出力する。どちらの処理を行うかはJS側が
        // DOMの中身（.nseb-status-bar の有無 / article[id^="post-"] の有無）を
        // 見て自分で判断する（PHP側でis_single()/is_page()による出し分けはしない）。
        // 【注記】当初は assets/*.js・*.css を wp_enqueue_script/style で外部ファイルとして
        // 読み込む構成にしていたが、本番環境（ConoHa WING）ではプラグインZIPインストール時に
        // サブディレクトリ（assets/）が展開されず、404（WordPressの通常の404ページ）に
        // なる事象を実機で確認した（実機検証 2026-08-10）。そのため元のv1.0.0と同じ、
        // プラグイン単一ファイル内にJS/CSSをインライン出力する方式に戻している。
        add_action('wp_head', array($this, 'print_frontend_css'));
        add_action('wp_footer', array($this, 'print_frontend_js'));

        // 単一記事ページでは、記事本文側に埋め込むボタンと重複しないよう
        // サイドバーのCodocサブスクウィジェットを非表示にする
        add_filter('widget_display_callback', array($this, 'maybe_hide_sidebar_subscription_widget'), 10, 3);

        // note記事（公開から90日以上経過したアーカイブ）の本文冒頭に、
        // 「当サイトで買うのが一番お得」というアピールボックスを挿入する。
        add_filter('the_content', array($this, 'prepend_archive_discount_box'));

        // 一覧カードの概要（抜粋）が、本文冒頭に挿入した赤い案内枠
        // （「💡この記事を単品で読みたい方へ」定型文）から始まってしまうため、
        // 投稿保存時に案内枠を除いた本文から抜粋(post_excerpt)を自動生成する
        // （Cocoonのカード概要はget_the_excerptフィルタを経由しないため、
        // フィルタではなくpost_excerptそのものを直接設定する方式にしている）。
        add_action('save_post', array($this, 'auto_generate_excerpt_on_save'), 10, 2);

        // Codoc価格・購入数キャッシュの定期更新（WP-Cron）
        add_action(self::CRON_HOOK, array($this, 'refresh_codoc_cache_all'));
        register_activation_hook(__FILE__, array(__CLASS__, 'on_activate'));
        register_deactivation_hook(__FILE__, array(__CLASS__, 'on_deactivate'));
        // 既に有効化済みのサイトでは register_activation_hook は再実行されないため、
        // 実行間隔を変更した場合（2026-08-14: twicedaily→hourly）はここで
        // 既存のcronイベントを検知して張り直す（wp_next_scheduled等はoptionsを
        // 読むだけの軽い呼び出しのため、毎回initで呼んでもコスト上問題ない）。
        add_action('init', array($this, 'maybe_upgrade_cron_schedule'));
    }

    public static function on_activate() {
        if (!wp_next_scheduled(self::CRON_HOOK)) {
            wp_schedule_event(time(), 'hourly', self::CRON_HOOK);
        }
    }

    /**
     * 2026-08-14 追記：購入数バッジが「決済してもずっと0のまま」に見える不具合の対応で、
     * Codocダッシュボード（アカウント設定・API関連ページ）を実機調査したが、
     * 決済完了イベントを能動的に通知するWebhook設定はCodoc側に存在しないことを確認した
     * （見つかったのは既存のCMS API用トークン表示のみ）。そのため「決済完了と同時に
     * WordPress側を更新する」真のリアルタイム連動は現状のCodoc APIでは実現できない。
     * 代替として、(1) 単一記事ページを開くたびにその記事1件分だけキャッシュを
     * ライブ更新する仕組み（handle_view参照）と、(2) 全記事の定期更新間隔を
     * twicedaily(1日2回)からhourly(1時間毎)へ短縮する対応の2本立てにした。
     */
    public function maybe_upgrade_cron_schedule() {
        $scheduled = wp_next_scheduled(self::CRON_HOOK);
        if ($scheduled && wp_get_schedule(self::CRON_HOOK) !== 'hourly') {
            wp_clear_scheduled_hook(self::CRON_HOOK);
            wp_schedule_event(time(), 'hourly', self::CRON_HOOK);
        } elseif (!$scheduled) {
            wp_schedule_event(time(), 'hourly', self::CRON_HOOK);
        }
    }

    public static function on_deactivate() {
        wp_clear_scheduled_hook(self::CRON_HOOK);
    }

    public function register_meta() {
        register_post_meta('post', self::LIKE_META_KEY, array(
            'type' => 'integer', 'single' => true, 'show_in_rest' => true, 'default' => 0,
        ));
        register_post_meta('post', self::CODOC_CACHE_PRICE_KEY, array(
            'type' => 'integer', 'single' => true, 'show_in_rest' => true, 'default' => 0,
        ));
        register_post_meta('post', self::CODOC_CACHE_PURCHASED_KEY, array(
            'type' => 'integer', 'single' => true, 'show_in_rest' => true, 'default' => 0,
        ));
        register_post_meta('post', self::CODOC_CACHE_UPDATED_KEY, array(
            'type' => 'integer', 'single' => true, 'show_in_rest' => true, 'default' => 0,
        ));
        register_post_meta('post', self::PRICE_BEFORE_DISCOUNT_KEY, array(
            'type' => 'integer', 'single' => true, 'show_in_rest' => true, 'default' => 0,
        ));
        // 既存のcodoc_entry_code（インポート時に設定済み）を読み取り専用でREST公開する。
        // キャッシュ更新対象記事の特定に使う。
        register_post_meta('post', 'codoc_entry_code', array(
            'type' => 'string', 'single' => true, 'show_in_rest' => true,
            'auth_callback' => '__return_false', // 書き込みは許可しない（読み取り専用）
        ));
    }

    /**
     * 全記事（codoc_entry_codeを持つもの）のCodoc価格・購入数キャッシュを更新する。
     * WP-Cronから1日2回自動実行される。ページ表示時には一切呼ばれない
     * （＝一覧ページの表示速度に影響しない）。
     */
    public function refresh_codoc_cache_all() {
        $post_ids = get_posts(array(
            'post_type' => 'post',
            'posts_per_page' => -1,
            'fields' => 'ids',
            'meta_query' => array(
                array('key' => 'codoc_entry_code', 'compare' => 'EXISTS'),
            ),
        ));
        foreach ($post_ids as $post_id) {
            $this->refresh_codoc_cache_for_post($post_id);
        }
    }

    private function refresh_codoc_cache_for_post($post_id) {
        $entry_code = get_post_meta($post_id, 'codoc_entry_code', true);
        if (!$entry_code) {
            return false;
        }
        $response = wp_remote_get(
            'https://codoc.jp/api/v1/storage/entries/' . rawurlencode($entry_code) . '/body.json',
            array('timeout' => 5)
        );
        if (is_wp_error($response) || wp_remote_retrieve_response_code($response) !== 200) {
            return false;
        }
        $body = json_decode(wp_remote_retrieve_body($response), true);
        $item = isset($body['entry']['item']) ? $body['entry']['item'] : array();
        update_post_meta($post_id, self::CODOC_CACHE_PRICE_KEY, isset($item['price']) ? (int) $item['price'] : 0);
        // 【2026-08-14 追記：保護ロジック】この公開API（body.json）のpurchased_countは、
        // 実際には決済が完了しているのにしばらく0のまま反映されないケースが実機で
        // 確認された（Codocダッシュボードの「売上」ページには購入番号付きで記録が
        // あるのに、このAPIだけ0を返し続けていた実例あり）。そのため、このAPIの値で
        // 既存キャッシュを「後退（減少）」させることは絶対に許さない。ダッシュボード
        // スクレイピング（backfill_codoc_verified_purchases.py）等、より正確な経路で
        // 先に正しい値が入っていた場合にこの不正確な公開APIで上書きしてしまう事故を防ぐ。
        $api_purchased = isset($item['purchased_count']) ? (int) $item['purchased_count'] : 0;
        $current_purchased = (int) get_post_meta($post_id, self::CODOC_CACHE_PURCHASED_KEY, true);
        update_post_meta($post_id, self::CODOC_CACHE_PURCHASED_KEY, max($api_purchased, $current_purchased));
        update_post_meta($post_id, self::CODOC_CACHE_UPDATED_KEY, time());
        return true;
    }

    /**
     * handle_view()専用：直近 LIVE_REFRESH_MIN_INTERVAL_SECONDS 秒以内に
     * 既に更新済みならスキップし、それより古い（または未取得）場合のみ
     * refresh_codoc_cache_for_post()を呼ぶ。無料記事（entry_codeが無い）は
     * refresh_codoc_cache_for_post側で即falseになるため、ここでも素通りさせる。
     */
    private function maybe_live_refresh_codoc_cache_for_post($post_id) {
        $updated_at = (int) get_post_meta($post_id, self::CODOC_CACHE_UPDATED_KEY, true);
        if ($updated_at > 0 && (time() - $updated_at) < self::LIVE_REFRESH_MIN_INTERVAL_SECONDS) {
            return false;
        }
        return $this->refresh_codoc_cache_for_post($post_id);
    }

    /**
     * ページ表示時に使う、キャッシュ済みpostmetaのみを読む版（外部APIへは一切アクセスしない）。
     * codoc_entry_code が無い記事（エキサイトブログ・TweetTV等の無料記事）は null を返す。
     */
    private function get_cached_codoc_info($post_id) {
        $entry_code = get_post_meta($post_id, 'codoc_entry_code', true);
        if (!$entry_code) {
            return null;
        }
        $updated_at = get_post_meta($post_id, self::CODOC_CACHE_UPDATED_KEY, true);
        if (empty($updated_at)) {
            // まだキャッシュが1度も更新されていない場合は、インポート時のcodoc_priceに
            // フォールバックする（購入数は取得済みキャッシュが無いためnull=不明表示）。
            $fallback_price = (int) get_post_meta($post_id, 'codoc_price', true);
            return array('price' => $fallback_price, 'purchased_count' => null);
        }
        return array(
            'price' => (int) get_post_meta($post_id, self::CODOC_CACHE_PRICE_KEY, true),
            'purchased_count' => (int) get_post_meta($post_id, self::CODOC_CACHE_PURCHASED_KEY, true),
        );
    }

    public function register_routes() {
        register_rest_route('engage/v1', '/like/(?P<id>\d+)', array(
            'methods' => 'POST',
            'callback' => array($this, 'handle_like'),
            'permission_callback' => '__return_true',
            'args' => array(
                'liked' => array('required' => true),
            ),
        ));
        // 一覧カード・単一記事ページ共通の動的データ取得エンドポイント。
        // 画面上に存在する記事IDをまとめて1回のリクエストで送ることで、
        // 記事ごとに個別リクエストするN+1問題を避ける
        // （単一記事ページでも「1記事だけのバッチ取得」として同じ経路を再利用する）。
        register_rest_route('engage/v1', '/card-data', array(
            'methods' => 'GET',
            'callback' => array($this, 'handle_card_data'),
            'permission_callback' => '__return_true',
            'args' => array(
                'ids' => array('required' => true),
            ),
        ));
        // 単一記事ページを実際に開いた際、PVを確実に+1記録するための専用エンドポイント。
        // 詳細は handle_view() のコメントを参照。
        register_rest_route('engage/v1', '/view/(?P<id>\d+)', array(
            'methods' => 'POST',
            'callback' => array($this, 'handle_view'),
            'permission_callback' => '__return_true',
        ));
    }

    /**
     * 【2026-08-12 追記：PVが加算されない不具合を受けて】
     * Cocoon純正のPVカウント経路（ページ本文中にCSS背景画像として埋め込まれた
     * 1x1ビーコン `lib/analytics/access.php?post_id=...&t=<PHP側で生成した時刻>`。
     * `logging_page_access()` を呼ぶだけの独立した軽量ブートストラップ）には、
     * このサーバー環境（ConoHa WING/nginx のページ全体キャッシュ）特有の
     * 致命的な弱点があることが実機調査で判明した：
     *
     *   ページ本体がnginxにキャッシュされると、そのHTML内に焼き込まれた
     *   ビーコンURL（`t=`パラメータ込み）もキャッシュされたまま固定される。
     *   このため、キャッシュが再生成されるまでの間、異なる訪問者が同じ
     *   キャッシュ済みページを開いても全員が「全く同じビーコンURL」を
     *   叩くことになり、nginxはそのURLへの2回目以降のリクエストを
     *   （PHPを実行せず）キャッシュから返してしまう
     *   （実機で確認済み：`X-Nginx-Cache: MISS`→`HIT`→`HIT`…と、
     *   2回目以降は`logging_page_access()`が一切実行されなくなる）。
     *   さらに、Cocoon純正の重複防止ロジック（同一IP・同一記事・同一日は
     *   1回しかカウントしない、という意図的かつ正しい仕様）と相まって、
     *   「管理者が動作確認のためリロードしても増えない」ように見え、
     *   ページキャッシュ由来の問題なのか仕様通りの重複防止なのかが
     *   非常に分かりにくい状態になっていた。
     *
     * 【対策】GETの画像ビーコン（＝URLがキャッシュキーになる）をやめ、
     * JS側で毎回その場で生成する一意なクエリ＋`cache:'no-store'`を付けた
     * POSTリクエストとしてこのエンドポイントを叩く方式に変更した。
     * POSTはそもそも一般的にキャッシュされない上、クエリも訪問のたびに
     * 変わるため、ページ本体がキャッシュされていても確実に毎回サーバーへ
     * 到達し、PHPが実行される。
     * カウントの記録先はCocoon純正の `logging_page_access()`
     * （＝Cocoon純正の`wp_cocoon_accesses`テーブル）をそのまま呼び出す
     * ことで一本化しており（新たな独自カウンターは作らない）、
     * 同一IP・同一記事・同一日の重複防止ロジックもCocoon側にそのまま
     * 委ねる（＝Cocoon純正のビーコンと同時に発火しても二重カウントには
     * ならない）。
     *
     * 【2026-08-14 追記：購入数のライブ更新】Codoc側に決済完了を能動的に
     * 通知するWebhookが存在しないため（maybe_upgrade_cron_scheduleのコメント
     * 参照）、このエンドポイントに「開かれた記事1件分だけ」Codocの価格・
     * 購入数キャッシュをその場でライブ更新する処理も相乗りさせた。全記事を
     * 毎回叩く一覧側（card-data）ではN+1になるため絶対にやってはいけないが、
     * ここは「今まさに開かれている1記事」だけなので許容できる。購入直後に
     * 詳細ページを開く（またはCodocの決済完了画面から記事に戻る）操作一つで、
     * ページ全体キャッシュの有無に関わらずバッジが最新化される。
     */
    public function handle_view($request) {
        $post_id = (int) $request['id'];
        if (get_post_status($post_id) !== 'publish' || get_post_type($post_id) !== 'post') {
            return new WP_Error('invalid_post', '対象の記事が見つかりません', array('status' => 404));
        }
        if (function_exists('logging_page_access')) {
            // logging_page_access()はグローバル$postのpost_typeにフォールバックする
            // 実装のため、REST APIコンテキストでも正しく動作するよう一時的に
            // セットアップする（get_cocoon_pv_breakdown()と同じパターン）。
            global $post;
            $original_post = $post;
            $post = get_post($post_id);
            if ($post) {
                setup_postdata($post);
                logging_page_access($post_id, 'post');
            }
            $post = $original_post;
            if ($original_post) {
                setup_postdata($original_post);
            } else {
                wp_reset_postdata();
            }
        }
        // codoc_entry_codeが無い記事（無料記事）では内部で即falseを返すだけなので
        // 無駄な外部アクセスは発生しない。直近で更新済みの場合もスキップする
        // （maybe_live_refresh_codoc_cache_for_postのコメント参照）。
        $this->maybe_live_refresh_codoc_cache_for_post($post_id);
        // card-data（get_card_badge_data）と全く同じデータ構造を返すことで、
        // フロントエンドの描画関数をそのまま共有できるようにする
        // （価格・購入数もこの応答に含まれ、直後のcard-data応答より新しい
        // ＝優先して使われる。initStatusBarのマージ処理を参照）。
        return $this->get_card_badge_data($post_id);
    }

    public function handle_card_data($request) {
        $ids_param = (string) $request->get_param('ids');
        $ids = array_filter(array_map('intval', explode(',', $ids_param)));
        $ids = array_slice(array_unique($ids), 0, 100); // 念のため上限を設ける

        $result = array();
        foreach ($ids as $post_id) {
            if (get_post_status($post_id) !== 'publish' || get_post_type($post_id) !== 'post') {
                continue;
            }
            $result[$post_id] = $this->get_card_badge_data($post_id);
        }
        return $result;
    }

    public function handle_like($request) {
        $post_id = (int) $request['id'];
        if (get_post_status($post_id) !== 'publish') {
            return new WP_Error('invalid_post', '対象の記事が見つかりません', array('status' => 404));
        }
        $liked = filter_var($request->get_param('liked'), FILTER_VALIDATE_BOOLEAN);

        $count = (int) get_post_meta($post_id, self::LIKE_META_KEY, true);
        $count = $liked ? $count + 1 : max(0, $count - 1);
        update_post_meta($post_id, self::LIKE_META_KEY, $count);

        return array('count' => $count);
    }

    /**
     * 単一記事ページのみ対象。the_title()はCocoonの<a title="...">属性生成でも
     * 内部的に同じフィルタを経由するため、一覧カードのバッジ付与にthe_titleフィルタを
     * 使うと属性値にHTMLがエスケープされたまま紛れ込んでしまう問題があった
     * （実機で確認済み）。単一記事ページのentry-title(h1)は属性生成に使われない
     * ため、この方式のままで問題ない。一覧カードのバッジは別途JS+REST方式
     * （nseb-frontend.js の initCardBadges）で表示する。
     */
    public function route_title_filter($title, $post_id = null) {
        if (is_admin() || !is_single() || !in_the_loop() || !is_main_query()) {
            return $title;
        }
        if (!$post_id) {
            $post_id = get_the_ID();
        }
        if (!$post_id || get_post_type($post_id) !== 'post') {
            return $title;
        }

        static $done_post_id = null;
        if ($done_post_id === $post_id) {
            return $title;
        }
        $done_post_id = $post_id;

        return $title . $this->build_status_bar_skeleton_html($post_id);
    }

    /**
     * PV（閲覧数）は、このプラグイン独自のカウンター（旧 view_count postmeta /
     * POST engage/v1/view/{id}）を廃止し、Cocoonテーマ純正のアクセスカウンター
     * （管理者専用フローティングパネルや一覧カードに「本日:/週:/月:/全体:」を
     * 表示している機能。lib/page-access/access-func.php の get_todays_pv() /
     * get_last_7days_pv() / get_last_30days_pv() / get_all_pv()）を単一の
     * データソースとして直接呼び出す方式に統一した。
     *
     * 【背景】この2つのPVカウンターが並存していたため、同じカード内で
     * テキスト表示（Cocoon純正、集計値）と👁️バッジ（このプラグイン独自の
     * postmetaカウンター）の数字が一致しない事故が発生した（実機・写真で報告）。
     * 原因はロジックの単純な不整合ではなく、そもそも別々の記録テーブル・
     * 別々のカウント条件（Cocoon純正は管理者・ボットのアクセスを除外して
     * カウントするが、旧postmetaカウンターは除外せずカウントしていたため、
     * 開発中のテストアクセスの分だけ大きくズレていた）による構造的な二重管理
     * だった。Cocoon純正の集計（管理者・ボット除外済みの、より正確な値）に
     * 一本化することで、テキストと👁️バッジは常に同じ関数呼び出しの結果を
     * 表示することになり、この種の不整合は構造的に起こり得なくなる。
     */
    private function get_cocoon_pv_breakdown($post_id) {
        $today = 0;
        $week = 0;
        $month = 0;
        $all = 0;
        if (function_exists('get_todays_pv') && function_exists('get_all_pv')) {
            // Cocoonのアクセスカウント関数はグローバル $post（ループ内の投稿）を
            // 前提にしているため、ループ外（REST APIコンテキスト）から呼ぶ際は
            // 一時的に $post をセットアップしてから呼び出し、必ず元に戻す。
            global $post;
            $original_post = $post;
            $post = get_post($post_id);
            if ($post) {
                setup_postdata($post);
                $today = (int) get_todays_pv($post_id);
                $week = (int) get_last_7days_pv($post_id);
                $month = (int) get_last_30days_pv($post_id);
                $all = (int) get_all_pv($post_id);
            }
            $post = $original_post;
            if ($original_post) {
                setup_postdata($original_post);
            } else {
                wp_reset_postdata();
            }
        }
        return array('today' => $today, 'week' => $week, 'month' => $month, 'all' => $all);
    }

    /**
     * 一覧カード用バッジのデータのみを返す（HTMLは組み立てない）。
     * 外部APIへは一切アクセスせず、postmetaに保存済みのキャッシュ値と
     * Cocoon純正のPV集計だけを読む。単一記事ページのステータスバーも
     * この同じデータ構造をJS側で描画に使う（＝一覧と詳細が「同じAPIから
     * 同じ値を読む」ため、表示の不整合が構造的に起きない）。
     */
    private function get_card_badge_data($post_id) {
        $codoc = $this->get_cached_codoc_info($post_id);
        if ($codoc === null) {
            $price = 0;
            $has_codoc = false;
        } else {
            $price = $codoc['price'];
            $has_codoc = true;
        }
        $pv = $this->get_cocoon_pv_breakdown($post_id);
        $like_count = (int) get_post_meta($post_id, self::LIKE_META_KEY, true);
        $purchased = ($has_codoc && $codoc['purchased_count'] !== null) ? $codoc['purchased_count'] : null;

        return array(
            'price' => $price,
            'price_label' => $price > 0 ? number_format($price) . '円' : '無料',
            'is_free' => $price <= 0,
            // view_count は全体PV（Cocoonの「全体:」と同一の値）。👁️バッジは
            // 常にこの値を表示するため、テキストの「全体:」と100%一致する。
            'view_count' => $pv['all'],
            'view_today' => $pv['today'],
            'view_week' => $pv['week'],
            'view_month' => $pv['month'],
            'like_count' => $like_count,
            'purchased_count' => $purchased,
        );
    }

    /**
     * 単一記事ページ・一覧ページ共通のCSS（インライン出力）。
     * PHP側はページ表示のたびに外部APIへアクセスすることもpostmetaの数値を
     * HTMLへ焼き込むこともしない（＝WPページキャッシュ・CDNキャッシュが効いていても
     * 古い数字がそのまま表示され続けることがない。数値は常にブラウザ側の
     * REST呼び出しで都度取得される）。
     */
    public function print_frontend_css() {
        if (is_admin()) {
            return;
        }
        ?>
<style id="nseb-frontend-css">
.nseb-status-bar{display:flex;flex-wrap:wrap;gap:.6em;align-items:center;margin:.6em 0 1.2em;padding:.7em .9em;background:#f8f8f8;border-radius:10px;font-size:.9em;color:#555;}
.nseb-stat{display:flex;align-items:center;gap:.35em;white-space:nowrap;}
.nseb-stat-icon{font-size:1.05em;line-height:1;}
.nseb-price{font-weight:bold;padding:.15em .7em;border-radius:999px;font-size:.95em;}
.nseb-price-free{background:#e6f7ee;color:#1a9c5c;}
.nseb-price-paid{background:#fff0e0;color:#e07b00;}
.nseb-like-btn{display:flex;align-items:center;gap:.3em;background:none;border:none;cursor:pointer;padding:0;font-size:1em;color:#555;}
.nseb-like-btn .nseb-heart{font-size:1.15em;transition:transform .15s ease;}
.nseb-like-btn.is-liked .nseb-heart{color:#e0245e;}
.nseb-like-btn:active .nseb-heart{transform:scale(1.3);}
.nseb-skeleton{opacity:.45;}
@media (max-width:600px){.nseb-status-bar{font-size:.8em;gap:.5em;padding:.6em .7em;}}
.nseb-card-badges{display:inline-flex;flex-wrap:wrap;gap:.4em;align-items:center;margin-left:.5em;vertical-align:middle;}
.nseb-card-badge{display:inline-flex;align-items:center;gap:.2em;font-size:.72em;font-weight:bold;padding:.1em .55em;border-radius:999px;white-space:nowrap;line-height:1.6;}
.nseb-card-badge.nseb-price-free{background:#e6f7ee;color:#1a9c5c;}
.nseb-card-badge.nseb-price-paid{background:#fff0e0;color:#e07b00;}
.nseb-card-badge.nseb-card-views{background:#f1f1f1;color:#666;}
.nseb-card-badge.nseb-card-like{background:#fdeef1;color:#e0245e;opacity:.75;}
.nseb-card-badge.nseb-card-like.is-liked{opacity:1;background:#fbdbe2;}
.nseb-card-badge.nseb-card-purchased{background:#f1f1f1;color:#666;}
.nseb-card-badge.nseb-card-purchased.is-purchased{background:#fbdbe2;color:#e0245e;}
.nseb-card-badge.nseb-card-purchased.is-readfree{background:#e3eefb;color:#2f6690;}
.nseb-stat-purchased.is-purchased{color:#e0245e;font-weight:bold;}
.nseb-stat-purchased.is-readfree{color:#2f6690;font-weight:bold;}
</style>
        <?php
    }

    /**
     * 単一記事ページ・一覧ページ共通のフロントエンドJS（インライン出力）。
     * どちらの処理を行うかはJS自身がDOMを見て判断する（initStatusBar / initCardBadges）。
     * 「スキを押した」状態はLocalStorage（nseb_liked_posts）で管理し、一覧・詳細の
     * 両方でこの同じヘルパーを参照する。
     */
    public function print_frontend_js() {
        if (is_admin()) {
            return;
        }
        $rest_root = esc_url_raw(rest_url('engage/v1'));
        ?>
<script id="nseb-frontend-js">
(function () {
  'use strict';

  var restRoot = <?php echo wp_json_encode($rest_root); ?>;
  // PHP側の定数（CONTENT_SUBSCRIPTION_DOM_ID）と単一の出所を保つため、
  // 文字列をJS側にハードコードせずここで注入する。
  var CONTENT_SUBSCRIPTION_DOM_ID = <?php echo wp_json_encode(self::CONTENT_SUBSCRIPTION_DOM_ID); ?>;
  var LIKED_KEY = 'nseb_liked_posts';
  // 2026-08-14追記：「購入済み」の個人状態をこのブラウザで記憶するためのキー。
  // 詳細ページを開いた際、Codoc自身の購入ウィジェット（.wp-block-codoc-codoc-block）が
  // 「購入ボタンを表示しない＝このブラウザは既に購入済み or 購読中」と判定した
  // 場合にのみセットする（checkCodocPurchaseState参照）。「いいね」と違い、
  // 自分でボタンを押して切り替えるものではなく、Codoc側の判定結果を観測して
  // 記録するだけの一方向のフラグ（一度立ったら消えない）。
  var PURCHASED_KEY = 'nseb_purchased_posts';
  // 2026-08-26追記：「サブスク加入中」であることをこのブラウザで記憶する
  // グローバルフラグ。個別記事ごとの購入済みフラグ（PURCHASED_KEY）とは別に、
  // 一度サブスク加入者と判定できたら、以後は一覧のどの有料記事カードも
  // （まだ一度も開いていない記事を含め）「🛒 購入済み」として扱ってよい
  // ようにするためのもの（effectivePurchased参照）。
  var ACTIVE_SUBSCRIBER_KEY = 'codoc_active_subscriber';
  // サブスク加入者かどうかを判定できる確実な信号（Codoc側の実際のDOM構造・
  // URLパラメータの実在）を、2026-08-26時点では検証できていない
  // （cms.js・cms-core.js を実際に取得し全文検索したが、依頼にあった
  // "codoc_conversion" というパラメータ名はどこにも登場しない）。
  // 未購読者を「購入済み」と誤判定すると売上機会を損なうため
  // （section 15と同種の、根拠の無いDOM推測による事故を避ける）、
  // 「このブラウザが異なる記事を複数（SUBSCRIBER_INFERENCE_THRESHOLD件）
  // 購入済み/アンロック済みと検出した」という、checkCodocPurchaseStateの
  // 既に実機検証済みのロジックだけを根拠にする、より保守的な代替ロジックを
  // 採用する。単体購入を2件以上した読者を誤検知するごく小さな可能性は残るが、
  // 根拠の無いDOM/URL推測よりはるかに安全側に倒した設計。
  var SUBSCRIBER_INFERENCE_THRESHOLD = 2;

  function isActiveSubscriber() {
    try { return window.localStorage.getItem(ACTIVE_SUBSCRIBER_KEY) === '1'; } catch (e) { return false; }
  }
  function markActiveSubscriber() {
    try { window.localStorage.setItem(ACTIVE_SUBSCRIBER_KEY, '1'); } catch (e) {}
  }
  // 依頼にあった「?codoc_conversion=subscription」URLパラメータの検知。
  // 実在するかどうか2026-08-26時点では確認できていないが、チェックすること
  // 自体に副作用は無いため保険として残す（実在しなければ単にこの分岐が
  // 一度も発火しないだけで、上記の代替ロジックが引き続き機能する）。
  function checkSubscriptionConversionParam() {
    try {
      var params = new URLSearchParams(window.location.search);
      if (params.get('codoc_conversion') === 'subscription') {
        markActiveSubscriber();
      }
    } catch (e) {}
  }

  function ready(fn) {
    if (document.readyState !== 'loading') { fn(); }
    else { document.addEventListener('DOMContentLoaded', fn); }
  }
  function fmt(n) {
    return (typeof n === 'number' ? n : 0).toLocaleString();
  }
  function parseCountText(text) {
    var digits = (text || '').replace(/[^0-9]/g, '');
    return digits ? parseInt(digits, 10) : 0;
  }

  // Cocoon純正の管理者専用PVパネル（本日:/週:/月:/全体:。単一記事ページでは
  // #admin-panel .admin-pv、一覧カードでは各article内の .admin-pv）を、
  // 👁️バッジと同じAPIレスポンス（card-data）の値で上書きする。これにより
  // テキスト表示と👁️バッジは常に同一のPHP関数（get_todays_pv等）の結果を
  // 表示することになり、両者が食い違うことは構造的に起こらなくなる。
  // 一般訪問者にはそもそも .admin-pv がDOMに存在しない（Cocoon側が
  // 管理者にのみサーバー側で出力するため）ので、その場合は何もしない。
  function syncAdminPvPanel(panel, d) {
    if (!panel || !d) { return; }
    var todayEl = panel.querySelector('.today-pv-count');
    var weekEl = panel.querySelector('.week-pv-count');
    var monthEl = panel.querySelector('.month-pv-count');
    var allEl = panel.querySelector('.all-pv-count');
    if (todayEl) { todayEl.textContent = fmt(d.view_today); }
    if (weekEl) { weekEl.textContent = fmt(d.view_week); }
    if (monthEl) { monthEl.textContent = fmt(d.view_month); }
    if (allEl) { allEl.textContent = fmt(d.view_count); }
  }

  function getLikedSet() {
    try {
      var raw = window.localStorage.getItem(LIKED_KEY);
      var arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr : [];
    } catch (e) { return []; }
  }
  function saveLikedSet(arr) {
    try { window.localStorage.setItem(LIKED_KEY, JSON.stringify(arr)); } catch (e) {}
  }
  function isLiked(postId) { return getLikedSet().indexOf(postId) !== -1; }
  function setLiked(postId, liked) {
    var set = getLikedSet();
    var idx = set.indexOf(postId);
    if (liked && idx === -1) { set.push(postId); }
    else if (!liked && idx !== -1) { set.splice(idx, 1); }
    saveLikedSet(set);
  }

  // 【論理整合性ガード】「スキ済み(赤点灯)」は必ず「カウント1以上」を伴う
  // という不変条件を、常にここで一元的に判定する。LocalStorageの
  // liked=true フラグだけでは信頼しない（カウント0の記事を赤点灯のまま
  // 表示する「論理的に不可能な状態」を防ぐため）。
  //   ・count が不明（undefined/null。まだAPI応答が来ていない）場合は
  //     LocalStorageの値をそのまま暫定表示に使う（初期描画の速さを優先）。
  //   ・count が確定していて 0 以下の場合は、LocalStorageの値に関わらず
  //     必ず「未点灯」とみなす。
  function effectiveLiked(postId, count) {
    var flagged = isLiked(postId);
    if (!flagged) { return false; }
    if (count === null || typeof count === 'undefined') { return true; }
    return count > 0;
  }

  // カウント0なのにLocalStorageだけ「スキ済み」のままになっている不整合
  // （前回スキ数だけをリセットした際にLocalStorage側の消し忘れがあった場合や、
  // POST /like が失敗したのに楽観的更新のフラグだけ残ってしまった場合等に
  // 発生しうる）を検知したら、その場でLocalStorageからも取り除いて自己修復する。
  function purgeStaleLikes(ids) {
    if (!ids || !ids.length) { return; }
    var set = getLikedSet();
    var changed = false;
    ids.forEach(function (id) {
      var idx = set.indexOf(id);
      if (idx !== -1) { set.splice(idx, 1); changed = true; }
    });
    if (changed) {
      saveLikedSet(set);
      console.warn('[nseb] スキ済みフラグとカウントの不整合(カウント0)を検知し自動補正しました: ids=' + ids.join(','));
    }
  }

  function getPurchasedSet() {
    try {
      var raw = window.localStorage.getItem(PURCHASED_KEY);
      var arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr : [];
    } catch (e) { return []; }
  }
  function savePurchasedSet(arr) {
    try { window.localStorage.setItem(PURCHASED_KEY, JSON.stringify(arr)); } catch (e) {}
  }
  function isPurchased(postId) { return getPurchasedSet().indexOf(postId) !== -1; }
  function markPurchased(postId) {
    var set = getPurchasedSet();
    if (set.indexOf(postId) === -1) {
      set.push(postId);
      savePurchasedSet(set);
    }
  }
  // 【2026-08-25 修正】「スキ」のeffectiveLikedと同じ「count<=0なら赤点灯させない」
  // ガードを以前は購入済みバッジにも適用していたが、これは前提が異なり誤りだった。
  // 「いいね」はこのサイト自身のカウンター（postLikeで自分がインクリメントした値を
  // 自分で読み返す）なので、count と「自分がいいね済みか」は同じデータソースに
  // 由来し常に整合する。一方、購入数バッジの count（d.purchased_count）は
  // Codocの公開API由来の集計値で、[[section 14で確認済みの通り実売上を反映せず
  // 0のまま止まることがある信頼できない値]]。対して「自分が購入済みか」の
  // フラグ（isPurchased）は checkCodocPurchaseState がCodocウィジェット自身の
  // DOM（ロック解除状態）を直接観測して立てる、count非依存のより確実な証拠。
  // そのため count<=0 を理由にこのフラグを不採用・削除（purge）すると、
  // 「ログイン済みで実際にロック解除されているのに、Codoc側の集計が0のままだと
  // 永久にグレー表示のまま」という今回報告された不具合を自ら発生させてしまう。
  // 購入済みフラグは count を一切参照せず、LocalStorageの観測結果をそのまま信用する。
  // 【2026-08-26追記】サブスク加入者（ACTIVE_SUBSCRIBER_KEY）と判定できている
  // 場合は、この記事個別の購入済みフラグが立っていなくても常に購入済み扱いにする
  // （サブスク加入者は全有料記事を読める、というサイト側の実際の権限設計に合わせる）。
  function effectivePurchased(postId) {
    return isActiveSubscriber() || isPurchased(postId);
  }

  // 【2026-08-26 追記：購入済みバッジの表示ロジックを一本化】以前は「色だけ
  // 赤くして文言は『0人が購入』のまま」という中途半端な実装になっており、
  // 「1人買ったのに0人？」という矛盾した見え方をしていた。詳細ページの
  // ステータスバーと一覧カードの両方が必ず同じ結論になるよう、判定ロジック
  // をこの1箇所に集約する。
  //
  // 【2026-08-28 追記：3段階表示への改修】以前は「このブラウザが購入済み/
  // 購読中」を1つのbooleanに畳み込んでいたが、単体購入とサブスク加入は
  // 読者から見て意味が異なる（買い切りの証拠 vs 定額での読み放題資格）ため、
  // 表示上も区別してほしいという要望を受けて、次の優先順位でモードを
  // 3つに分けた：
  //   mode='purchased'（赤）: この記事個別のアンロックを実際に検出済み
  //     （checkCodocPurchaseStateの実機検証済みロジック）。サブスク加入者
  //     であっても、その記事を個別に購入していればこちらを優先表示する
  //     （「この記事は買い切った」という、より具体的な事実の方が有益なため）。
  //   mode='readfree'（青）: 個別購入は未検出だが、サブスク加入者と推定
  //     できている（isActiveSubscriber）。
  //   mode='count'（グレー）: どちらでもない場合、購入者数が1人以上なら
  //     人数を表示。0人（または無料記事等でカウント自体が無い＝
  //     null/undefined）の場合は、ネガティブな「0人が購入」を読者に
  //     見せないためバッジ自体を非表示にする。
  // purchasedCountがnull/undefined（＝Codocエントリー自体が無い＝無料記事）
  // の場合は、individuallyPurchased/isSubscriberの値に関わらず常に非表示にする
  // （サブスクは有料記事を読み放題にする権利であって無料記事とは無関係なため）。
  function computeAccessBadgeState(purchasedCount, individuallyPurchased, isSubscriber) {
    var isPaidArticle = purchasedCount !== null && typeof purchasedCount !== 'undefined';
    if (!isPaidArticle) {
      return { show: false, mode: null };
    }
    if (individuallyPurchased) {
      return { show: true, mode: 'purchased' };
    }
    if (isSubscriber) {
      return { show: true, mode: 'readfree' };
    }
    return { show: purchasedCount >= 1, mode: 'count' };
  }

  // 詳細ページのステータスバー用。既存の<span>要素を使い回し、非表示時は
  // hidden属性とstyle.displayの両方で確実に隠す（Cocoon側のCSSリセットに
  // よる[hidden]の無効化を警戒し、インラインstyleでも重ねて指定する）。
  // 元の価格バッジ（nseb-price）は消さずそのまま併記する仕様のため、この
  // 関数はアクセス状態バッジ単体の中身だけを組み立てる。
  function renderAccessStat(el, state, purchasedCount) {
    if (!el) { return; }
    el.hidden = !state.show;
    el.style.display = state.show ? '' : 'none';
    el.classList.remove('is-purchased', 'is-readfree');
    el.textContent = '';
    if (!state.show) { return; }
    var icon = document.createElement('span');
    icon.className = 'nseb-stat-icon';
    var label;
    if (state.mode === 'purchased') {
      el.classList.add('is-purchased');
      icon.textContent = '🛒';
      label = '購入済み';
    } else if (state.mode === 'readfree') {
      el.classList.add('is-readfree');
      icon.textContent = '📖';
      label = '読み放題';
    } else {
      icon.textContent = '🛒';
      label = fmt(purchasedCount) + '人が購入';
    }
    el.appendChild(icon);
    el.appendChild(document.createTextNode(label));
  }

  // 【2026-08-25追記：購入済み/アンロック後もサブスク誘導バナーが残る不具合の
  // 修正】Codoc自身のペイウォールは正しく解除され本文は最後まで読めているのに、
  // 本文冒頭の無料プレビューエリアに置かれた「単品で買うより断然お得なサブスク」
  // 誘導枠（赤枠。auto_sync_blogs.pyがcodoc-blockの手前に挿入する定型ブロック）と、
  // アーカイブ割引ボックス（.archive-discount-box）が、購入・アンロック後も
  // そのまま表示され続けていたため、読者が「まだ課金が必要なのか」と誤解する
  // 事故が実機（スマホ）の報告で判明した。購入済みと判定できた時点でこの2つを
  // 非表示にする。
  //
  // 赤枠サブスク誘導ボックスにはクラス名が付いていないため、本文中に唯一
  // 埋め込まれるサブスクウィジェット自身（CONTENT_SUBSCRIPTION_DOM_ID。
  // strip_upsell_box()のマーカーと同じ値）を起点に、.entry-content の直下の
  // 子要素になるまで祖先を遡って特定する（サイドバー側にも同じIDの別要素が
  // 存在するため、必ず.entry-content内に絞り込んだ上で起点を探す）。
  function hideUpsellBanners() {
    var content = document.querySelector('.entry-content');
    if (!content) { return; }

    var discountBox = content.querySelector('.archive-discount-box');
    if (discountBox) { discountBox.style.display = 'none'; }

    var widget = content.querySelector('#' + CONTENT_SUBSCRIPTION_DOM_ID);
    if (widget) {
      var node = widget;
      while (node && node.parentElement && node.parentElement !== content) {
        node = node.parentElement;
      }
      if (node && node.parentElement === content) {
        node.style.display = 'none';
      }
    }
  }

  // 【2026-08-14追記：購入済み状態の検出】このサイトにはログイン機能が無いため
  // （section 10のスキと同じ制約）、「このブラウザが過去に買ったか」を
  // サーバー側で直接知る手段は無い。唯一の手がかりは、Codoc自身の購入ウィジェット
  // （記事詳細ページにだけ埋め込まれる .wp-block-codoc-codoc-block）が、
  // このブラウザに対して「購入ボタン（記事を購入／購入手続き）」を表示するかどうか。
  // Codocはこの判定を、購入直後のセッションCookie、またはCodocへのログイン状態
  // （購読中を含む）を見て行っている（実機確認：ウィジェットのDOM内に
  // 「ログインして購入を復元」という導線が存在し、購入・購読の権利は
  // Codoc側のログイン状態に紐づく設計であることが分かる）。そのため、
  // ボタンが消えている＝このブラウザは単体購入または月額サブスクで
  // アクセス権を持っている、とみなせる。Codocのウィジェットは自身のJS
  // （cms.js、defer属性）で非同期にDOMを書き換えるため、単純に一度だけ
  // チェックするのではなくMutationObserverで監視し、初回描画・購入完了後の
  // 動的な変化のどちらも取りこぼさないようにする。
  function checkCodocPurchaseState(postId) {
    var container = document.querySelector('.wp-block-codoc-codoc-block');
    if (!container) { return; } // 無料記事、またはCodocブロックが無いページ

    // 【2026-08-14 追記：実際の購入者ブラウザで再現した不具合の修正】
    // 実際にこの記事を購入したユーザー（Codocのオーディエンス/売上ページに
    // 記録されている実在の購入者）の実機HTMLを提供してもらい調査した結果、
    // ロック解除状態でも `.codoc-buy-wrap` 要素自体は削除されず、
    // インラインstyleで `display: none` にされて非表示になるだけ、と判明した
    // （当初「要素が存在しない」ことを条件にしていたため、要素は存在し続ける
    // ＝常に「未購入」と誤判定していた＝実際の購入者でも一切赤くならない
    // 不具合の直接の原因だった）。あわせて、ロック解除状態では
    // (a) 購入者名を表示する `.codoc-user`（例：`<div class="codoc-user">
    // <strong>忍者トゥルーサー</strong></div>`）が出現する、
    // (b) 本文の続きを差し込む `.codoc-entry-body-after` に実際の記事本文が
    // 流し込まれる（ロック中は同じ要素が存在するが中身は空）、
    // という2つの追加の陽性シグナルがあることも実機HTMLで確認できた。
    // 以降はこの3つのいずれかを満たせば購入済みと判定する（いずれも
    // ロック中の実機HTMLでは満たされないことを確認済みのシグナルのみを使う）。
    function isHidden(el) {
      if (!el) { return true; } // 要素自体が無い＝購入ボタンを出していない
      if (el.style && el.style.display === 'none') { return true; }
      try { return window.getComputedStyle(el).display === 'none'; } catch (e) { return false; }
    }
    function evaluate() {
      // 【実機で確認した別の不具合の修正】当初は「子要素が1つでもあれば
      // 判定してよい」としていたが、Codocのウィジェット（cms.js）はデータ
      // 取得中に一瞬だけ購入ボタンを含まない小さなプレースホルダを描画する
      // ことが実機調査で判明した（匿名の初回訪問者でも、生成から151ms後に
      // 本来のロック画面（購入ボタン付き）に差し替わるまでの間、一瞬だけ
      // 「ボタンが無い状態」が存在する）。この一瞬を「購入済み」と誤検知し、
      // 実際には一度も購入していない匿名訪問者にまで購入済みフラグが
      // 立ってしまう事故をPlaywrightのテストで再現・特定した。
      // 対策として、Codocウィジェットの描画が完全に終わったことを示す
      // 目印（.codoc-copyright、"powered by codoc"のフッター。ロック中の
      // 最終状態では必ず描画されることを確認済み）が無い間は一切判定しない
      // ようにした。ただし、ロック解除状態（実際の購入者の実機HTMLで確認）に
      // このフッターが同じ形で出るかまでは未確認のため、そちらの見逃しを
      // 防ぐ保険として「中身の文字数がプレースホルダ（44文字程度）より
      // 十分大きい」ことでも代替的に判定可（コンテナの中身がどちらの
      // 最終状態でも2500文字を超えることは実機で確認済みで、44文字の
      // プレースホルダとは十分な差がある）。
      var htmlLen = container.innerHTML.length;
      if (!container.querySelector('.codoc-copyright') && htmlLen < 500) { return; }

      var buyWrapHidden = isHidden(container.querySelector('.codoc-buy-wrap'));
      var hasUser = !!container.querySelector('.codoc-user');
      var bodyAfterEl = container.querySelector('.codoc-entry-body-after');
      var hasUnlockedBody = !!(bodyAfterEl && bodyAfterEl.textContent && bodyAfterEl.textContent.trim().length > 0);

      if (buyWrapHidden || hasUser || hasUnlockedBody) {
        markPurchased(postId);
        hideUpsellBanners();
        // 【2026-08-26追記】このブラウザが異なる記事を複数（閾値以上）
        // 購入済み/アンロック済みと検出した場合、サブスク加入者である可能性が
        // 高いとみなす（詳細はACTIVE_SUBSCRIBER_KEY定義部のコメント参照）。
        if (getPurchasedSet().length >= SUBSCRIBER_INFERENCE_THRESHOLD) {
          markActiveSubscriber();
        }
      }
    }
    evaluate();

    if (typeof MutationObserver !== 'undefined') {
      var observer = new MutationObserver(function () { evaluate(); });
      observer.observe(container, { childList: true, subtree: true });
      // 購入完了直後の遷移（決済フローが完了しCodoc側がDOMを書き換える等）を
      // 拾うのに十分な時間だけ監視し、その後は監視をやめてリソースを解放する。
      window.setTimeout(function () { observer.disconnect(); }, 30000);
    }
  }

  function fetchCardData(ids) {
    if (!restRoot || !ids.length) { return Promise.resolve({}); }
    // cache: 'no-store' と時刻クエリで、ブラウザ・中間プロキシ・ConoHa WINGの
    // ページキャッシュ層による古いレスポンスの再利用を確実に防ぐ
    // （PHPの静的出力キャッシュを完全にバイパスする要件）。
    return fetch(restRoot + '/card-data?ids=' + ids.join(',') + '&_=' + Date.now(), { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : {}; })
      .catch(function () { return {}; });
  }
  // 単一記事ページが開かれたときにPVを+1記録する。GETの画像ビーコンではなく
  // POST（そもそもキャッシュ対象にならない）＋毎回一意なURLにすることで、
  // ページ本体がnginx等にキャッシュされていても確実にサーバーへ到達する
  // （handle_view()のコメント参照）。keepalive:trueは、ページ表示直後に
  // 訪問者がすぐ離脱した場合でもリクエストがバックグラウンドで完走するように
  // するため（postLikeと同じ理由）。
  function postView(postId) {
    return fetch(restRoot + '/view/' + postId + '?_=' + Date.now(), {
      method: 'POST', cache: 'no-store', keepalive: true,
    }).then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }
  // postLikeの戻り値は { ok, confirmed, data } の形にする。
  //   ok=true                        : 成功（data.countを利用可）
  //   ok=false, confirmed=true       : サーバーから明確な失敗応答（4xx/5xx）が
  //                                     返ってきた「確定的な失敗」
  //   ok=false, confirmed=false      : fetch自体が例外を投げた「不明な失敗」
  // 【なぜ区別するのか】いいねボタンを押した直後にユーザーがページ遷移すると
  // （実機のPlaywrightテストで「クリック直後にブラウザバック」を検証した際に
  // 再現した）、離脱中のドキュメントのfetchはブラウザに中断され
  // 「TypeError: Failed to fetch」で例外を投げる。keepalive: true を付けると
  // リクエスト自体はサーバーまでバックグラウンドで届く（サーバー側は正しく
  // カウントを増やす）が、クライアント側は結局レスポンスを受け取れないまま
  // 例外になる。この「不明な失敗」を確定的な失敗と同列に扱ってLocalStorageの
  // 楽観的更新をロールバックすると、「サーバー側は+1したのにLocalStorageは
  // 未いいねに逆戻りする」という新たな不整合を自ら生んでしまうことが実機で
  // 確認された。そのため「不明な失敗」ではロールバックせず、次回ページ表示時の
  // 認証データ照合（renderCardBadges の自己修復ロジック）に判定を委ねる。
  // 一方、サーバーが明確に失敗応答を返した「確定的な失敗」は安心して
  // ロールバックしてよい（そのリクエストがサーバーに副作用を与えていないと
  // 確信できるため）。
  function postLike(postId, liked) {
    return fetch(restRoot + '/like/' + postId, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ liked: liked }),
      keepalive: true,
    }).then(function (r) {
      if (!r.ok) { return { ok: false, confirmed: true }; }
      return r.json().then(function (data) { return { ok: true, data: data }; });
    }).catch(function () {
      return { ok: false, confirmed: false };
    });
  }

  // 【bfcache対策の要点】ブラウザの「戻る」操作でこのページへ復元される際、
  // 直前に離脱した瞬間のDOMスナップショットがそのまま再表示される
  // （bfcache = back/forward cache。この場合 DOMContentLoaded は発火せず、
  // このスクリプトも再実行されない）ことが実機で確認されている。つまり
  // 詳細ページで「いいね」を押した直後に戻ると、一覧ページは訪問前の
  // 古いバッジ（未いいね状態）のまま固まって見える。
  // 対策として、以下の initStatusBar / initCardBadges は何度呼び出しても
  // 安全な「冪等」関数として実装し（クリックリスナーの二重登録を防ぎ、
  // DOM要素も使い回して重複追加しない）、通常の初回ロード（ready）だけでなく
  // pageshowイベントでも必ず呼び直す。これにより、
  //   1) まずLocalStorageを見て「いいね」ハートの見た目だけ即座に書き換え、
  //   2) 続けてバッチAPI（card-data）を叩いて最新の数値でDOMを再描画する
  // という2段階の同期をリロード無しで完成させる。

  function initStatusBar() {
    var bar = document.querySelector('.nseb-status-bar[data-post-id]');
    if (!bar) { return; }

    var h1 = bar.closest('h1');
    if (h1 && h1.parentNode && h1.nextSibling !== bar) {
      h1.parentNode.insertBefore(bar, h1.nextSibling);
    }

    var postId = parseInt(bar.getAttribute('data-post-id'), 10);
    if (!postId) { return; }

    var btn = bar.querySelector('.nseb-like-btn');

    // count を省略した場合（まだAPI応答が来ていない初回描画）はLocalStorageの
    // フラグをそのまま信用するが、countが分かっている場合は
    // effectiveLiked()により「カウント0なのに赤点灯」を必ず防ぐ。
    function renderHeart(count) {
      if (!btn) { return false; }
      var heartEl = btn.querySelector('.nseb-heart');
      var liked = effectiveLiked(postId, count);
      if (liked) {
        btn.classList.add('is-liked');
        if (heartEl) { heartEl.textContent = '♥'; }
      } else {
        btn.classList.remove('is-liked');
        if (heartEl) { heartEl.textContent = '♡'; }
      }
      return liked;
    }
    // LocalStorageを最優先で参照し、ネットワーク応答を待たずに即座にハートを描画する。
    renderHeart();

    // Codoc自身の購入ウィジェットを監視し、このブラウザが購入済み/購読中と
    // 分かったら購入済みフラグを記録する（checkCodocPurchaseStateのコメント参照）。
    checkCodocPurchaseState(postId);
    var purchasedEl = bar.querySelector('.nseb-stat-purchased');
    // 【2026-08-27追記】無料記事（data-paid="0"）では、サブスク加入者一括判定
    // だけを根拠に「🛒 購入済み」を早期描画しない（無料記事はそもそもサブスクの
    // 対象外のため）。有料記事かどうかはスケルトン生成時にPHP側で焼き込んだ
    // data-paid属性で判定する（build_status_bar_skeleton_html参照）。
    var isPaidArticle = bar.getAttribute('data-paid') === '1';
    if (isPaidArticle && effectivePurchased(postId)) {
      // ネットワーク応答を待たず、既知のフラグだけで即座に「🛒 購入済み」
      // または「📖 読み放題」を描画し、サブスク誘導バナーも即座に隠しておく
      // （カウントにもネットワーク応答にも依存しないため、待つ理由が無い。
      // どちらのモードかはLocalStorageの2つのフラグだけで判定でき、
      // これもネットワーク応答を待たずに分かる）。
      if (purchasedEl) {
        purchasedEl.classList.remove('nseb-skeleton');
        var earlyMode = isPurchased(postId) ? 'purchased' : 'readfree';
        renderAccessStat(purchasedEl, { show: true, mode: earlyMode }, null);
      }
      hideUpsellBanners();
    }

    // postView(postId) は「このページを開いた」というPVを実際に+1記録し、
    // ついでにこの1記事分だけCodocの価格・購入数キャッシュもライブ更新する
    // 副作用付きの呼び出し（handle_view()参照。2026-08-14: 購入数のリアルタイム
    // 反映のため、価格・購入数・スキ数を含むcard-dataと同じ形式のデータを
    // 返すよう拡張した）。fetchCardData([postId]) は並行実行されるため、
    // postViewの副作用（ライブ更新）が反映される前のタイミングで応答が返り
    // 1件古い値になっている可能性がある。そのため両者が揃った際は、
    // より新しいpostView側の値を常に優先して上書きする。
    Promise.all([postView(postId), fetchCardData([postId])]).then(function (results) {
      var viewData = results[0];
      var cardData = results[1] || {};
      var d = cardData[postId] || cardData[String(postId)] || {};
      // PV系フィールドだけでなく価格・購入数・スキ数もpostView応答の方が
      // 新しいため、丸ごと上書きする。
      if (viewData) {
        d = Object.assign({}, d, viewData);
      }
      var likeCount = typeof d.like_count === 'number' ? d.like_count : 0;

      var viewEl = bar.querySelector('.nseb-view-count');
      if (viewEl) {
        // 👁️バッジ（全体PV）は、Cocoon純正のPVカウンター「全体:」と同じ値
        // （view_count = get_all_pv()）を表示するため常に一致する。
        viewEl.textContent = fmt(d.view_count);
        viewEl.classList.remove('nseb-skeleton');
      }
      syncAdminPvPanel(document.querySelector('#admin-panel .admin-pv'), d);

      var priceEl = bar.querySelector('.nseb-price');
      if (priceEl && d.price_label) {
        priceEl.textContent = d.price_label;
        priceEl.classList.remove('nseb-skeleton', 'nseb-price-free', 'nseb-price-paid');
        priceEl.classList.add(d.is_free ? 'nseb-price-free' : 'nseb-price-paid');
      }

      var likeCountEl = bar.querySelector('.nseb-like-count');
      if (likeCountEl) {
        likeCountEl.textContent = fmt(likeCount);
        likeCountEl.classList.remove('nseb-skeleton');
      }

      // サーバー側の実カウントが確定したので、それを根拠に最終判定し直す。
      // LocalStorageが「スキ済み」のままなのにカウントが0だった場合は、
      // 表示を未点灯に補正した上でLocalStorage側も自動で消しておく
      // （不整合データの自己修復）。
      var wasFlagged = isLiked(postId);
      renderHeart(likeCount);
      if (wasFlagged && likeCount <= 0) {
        purgeStaleLikes([postId]);
      }

      if (purchasedEl) {
        purchasedEl.classList.remove('nseb-skeleton');
        var accessState = computeAccessBadgeState(d.purchased_count, isPurchased(postId), isActiveSubscriber());
        renderAccessStat(purchasedEl, accessState, d.purchased_count);
      }
    });

    // pageshowでの再実行時にクリックリスナーが二重登録されクリック1回で
    // いいね数が2ずつ増減するのを防ぐため、bound済みフラグで一度だけ登録する。
    if (btn && !btn.dataset.nsebBound) {
      btn.dataset.nsebBound = '1';
      btn.addEventListener('click', function () {
        var likeCountEl = bar.querySelector('.nseb-like-count');
        var prevLiked = isLiked(postId);
        var nextLiked = !prevLiked;
        var prevCount = parseCountText(likeCountEl ? likeCountEl.textContent : '');
        var optimisticCount = nextLiked ? prevCount + 1 : Math.max(0, prevCount - 1);

        // 楽観的更新：サーバー応答を待たず、ハートとカウントを即座に
        // 一体で書き換える（赤点灯と同時に必ず+1された数字を保持する）。
        setLiked(postId, nextLiked);
        renderHeart(optimisticCount);
        if (likeCountEl) {
          likeCountEl.textContent = fmt(optimisticCount);
          likeCountEl.classList.remove('nseb-skeleton');
        }

        postLike(postId, nextLiked).then(function (result) {
          if (result.ok) {
            // サーバーの実カウントを正として上書きする（楽観値との食い違いを解消）。
            if (likeCountEl) { likeCountEl.textContent = fmt(result.data.count); }
            renderHeart(result.data.count);
            if (nextLiked && result.data.count <= 0) {
              purgeStaleLikes([postId]);
            }
            return;
          }
          if (result.confirmed) {
            // サーバーが明確に失敗応答を返した場合のみ、安心してロールバックする
            // （このリクエストがサーバー側に副作用を残していないと確信できるため）。
            setLiked(postId, prevLiked);
            renderHeart(prevCount);
            if (likeCountEl) { likeCountEl.textContent = fmt(prevCount); }
            return;
          }
          // ここに来るのは「fetch自体が例外を投げた＝不明な失敗」の場合のみ
          // （postLikeのコメント参照）。ページ遷移によるものである可能性が高く、
          // サーバー側には実際に反映されている場合があるため、ここでは
          // ロールバックしない。楽観的更新はそのまま残し、次回このページを
          // 表示した際の renderCardBadges / initStatusBar の認証データ照合に
          // 最終判定を委ねる（不整合が残っても自己修復される設計）。
        });
      });
    }
  }

  // 一覧カードの「いいね」バッジだけを、LocalStorageの内容に合わせて
  // 即座に（ネットワーク応答を待たずに）書き換える。まだバッジ自体が
  // 存在しない初回描画前の状態では何もしない（fetchCardData側の初期描画に任せる）。
  // 【意図的にcountでゲートしない】ここで参照できる「現在表示中の数字」は、
  // bfcache復元直後は「詳細ページを訪れる前の古い数字」であり、真偽の判定に
  // 使えない（＝ここでcountをガードに使うと、直前に本当にスキした記事まで
  // 一瞬「未点灯」に見えてしまう回帰バグになることを実機で確認した）。
  // そのためこの関数はあくまで「ネットワーク応答が届くまでの暫定的な
  // 楽観的表示」と割り切ってLocalStorageを無条件に信頼し、直後に必ず走る
  // renderCardBadges()（サーバーの実カウントで最終判定・自己修復する）に
  // よって数百ms以内に必ず正しい状態へ収束させる。
  function paintCardLikeFromCache(idToMetaEl) {
    var likedSet = getLikedSet();
    Object.keys(idToMetaEl).forEach(function (id) {
      var wrap = idToMetaEl[id].querySelector('.nseb-card-badges');
      if (!wrap) { return; }
      var likeBadge = wrap.querySelector('.nseb-card-like');
      if (!likeBadge) { return; }
      var liked = likedSet.indexOf(parseInt(id, 10)) !== -1;
      var countText = likeBadge.textContent.replace(/^[♥♡]/, '');
      likeBadge.classList.toggle('is-liked', liked);
      likeBadge.textContent = (liked ? '♥' : '♡') + countText;
    });
  }

  // バッチAPI（card-data）の最新レスポンスで、カード内の全バッジ（価格・PV・
  // いいね・購入数）を再描画する。既存の .nseb-card-badges 要素があれば
  // 使い回して中身だけ更新する（pageshowでの再実行時にバッジが重複追加
  // されるのを防ぐ）。
  // 【論理整合性ガード】ここではサーバーの実カウント(d.like_count)が
  // 確定しているため、これを根拠に最終判定する。LocalStorageが
  // 「スキ済み」のままなのにカウントが0の記事があれば、表示を未点灯に
  // 補正した上でLocalStorage側も自動で消す（不整合データの自己修復）。
  function renderCardBadges(idToMetaEl, data) {
    var staleIds = [];
    Object.keys(data).forEach(function (id) {
      var metaEl = idToMetaEl[id];
      if (!metaEl) { return; }
      var d = data[id];
      var numericId = parseInt(id, 10);
      var likeCount = typeof d.like_count === 'number' ? d.like_count : 0;
      var wasFlagged = isLiked(numericId);
      var liked = effectiveLiked(numericId, likeCount);
      if (wasFlagged && !liked) { staleIds.push(numericId); }

      // このカード内にCocoon純正の管理者専用PVパネルがあれば（一般訪問者には
      // 存在しない）、👁️バッジと同じデータで同期する。
      var article = metaEl.closest('article');
      if (article) {
        syncAdminPvPanel(article.querySelector('.admin-pv'), d);
      }

      var wrap = metaEl.querySelector('.nseb-card-badges');
      if (!wrap) {
        wrap = document.createElement('span');
        wrap.className = 'nseb-card-badges';
        metaEl.appendChild(wrap);
      }
      wrap.innerHTML = '';

      // 【2026-08-28修正】アクセス状態バッジ（購入済み/読み放題/購入者数）は
      // 価格バッジの直前に表示する（元の価格は消さずそのまま併記する仕様。
      // computeAccessBadgeStateのコメント参照）。
      var accessState = computeAccessBadgeState(d.purchased_count, isPurchased(numericId), isActiveSubscriber());
      if (accessState.show) {
        var accessBadge = document.createElement('span');
        var accessLabel;
        if (accessState.mode === 'purchased') {
          accessBadge.className = 'nseb-card-badge nseb-card-purchased is-purchased';
          accessLabel = '🛒購入済み';
        } else if (accessState.mode === 'readfree') {
          accessBadge.className = 'nseb-card-badge nseb-card-purchased is-readfree';
          accessLabel = '📖読み放題';
        } else {
          accessBadge.className = 'nseb-card-badge nseb-card-purchased';
          accessLabel = '🛒' + fmt(d.purchased_count);
        }
        accessBadge.textContent = accessLabel;
        wrap.appendChild(accessBadge);
      }

      var priceBadge = document.createElement('span');
      priceBadge.className = 'nseb-card-badge nseb-price ' + (d.is_free ? 'nseb-price-free' : 'nseb-price-paid');
      priceBadge.textContent = d.price_label;
      wrap.appendChild(priceBadge);

      var viewBadge = document.createElement('span');
      viewBadge.className = 'nseb-card-badge nseb-card-views';
      viewBadge.textContent = '👁' + fmt(d.view_count);
      wrap.appendChild(viewBadge);

      var likeBadge = document.createElement('span');
      likeBadge.className = 'nseb-card-badge nseb-card-like' + (liked ? ' is-liked' : '');
      likeBadge.textContent = (liked ? '♥' : '♡') + fmt(d.like_count);
      wrap.appendChild(likeBadge);
    });
    purgeStaleLikes(staleIds);
  }

  // 【2026-08-28追記：サイドバー検索欄の「購入済み」「スキ」絞り込み】
  // サーバー側は購入・スキ状態を一切知らない（LocalStorageのみに存在する
  // データのため）ので、これはサーバーへの再検索ではなく、今表示されている
  // 一覧のarticle要素をクライアント側で即座に表示/非表示するだけの機能にする。
  // 判定は、独自にLocalStorageを読み直すのではなく、renderCardBadgesが
  // 既に描画済みの各カードのバッジのクラス（.is-purchased/.is-readfree/
  // .is-liked）をそのまま見る。これによりcomputeAccessBadgeState等の
  // 判定ロジックと必ず一致し、二重実装によるズレが起きない。
  // チェックボックス自体は検索フォームウィジェット（custom_html-3、
  // このリポジトリには無い）側の静的HTMLとして追加済みで、name属性を
  // 持たないため既存のfilter_cats[]送信・同期処理には一切干渉しない。
  //
  // 【2026-08-29修正】「購入済み」フィルターに、サブスク加入による読み放題
  // 対象（.is-readfree）まで含まれてしまっていた不具合を修正。「購入済み」は
  // 単体購入（買い切り）の履歴が実際にある記事（.is-purchased）だけを対象と
  // し、.is-readfreeは対象外にする（サブスク加入者であっても、その記事を
  // 個別購入していなければ「購入済み」検索には出さない）。
  function applyMetaFilters() {
    var purchasedCb = document.getElementById('nseb-filter-purchased');
    var likedCb = document.getElementById('nseb-filter-liked');
    if (!purchasedCb && !likedCb) { return; }
    var wantPurchased = !!(purchasedCb && purchasedCb.checked);
    var wantLiked = !!(likedCb && likedCb.checked);

    var articles = document.querySelectorAll('article[id^="post-"]');
    var anyVisible = false;
    articles.forEach(function (art) {
      if (!wantPurchased && !wantLiked) {
        art.style.display = '';
        anyVisible = true;
        return;
      }
      var matchesPurchased = wantPurchased && !!art.querySelector('.nseb-card-purchased.is-purchased');
      var matchesLiked = wantLiked && !!art.querySelector('.nseb-card-like.is-liked');
      var show = matchesPurchased || matchesLiked;
      art.style.display = show ? '' : 'none';
      if (show) { anyVisible = true; }
    });

    // 【2026-08-29追記：クライアント側限定の絞り込みの限界を補う案内】
    // 購入済み/スキ絞り込みはサーバー側のページネーションとは無関係に、
    // 今表示中のページ内だけでshow/hideしているため、「このページには
    // 該当記事が無い（他のページにはあるかもしれない）」という状態が起こり
    // うる。何も表示されない空白画面のまま放置すると壊れて見えるため、
    // その場合のみ案内文を出す（articleの増減はしないため既存レイアウトは
    // 壊さない）。
    var noticeId = 'nseb-meta-filter-empty-notice';
    var existingNotice = document.getElementById(noticeId);
    var listContainer = articles.length ? articles[0].closest('#list, .list') : null;
    if ((wantPurchased || wantLiked) && !anyVisible && listContainer) {
      if (!existingNotice) {
        var notice = document.createElement('p');
        notice.id = noticeId;
        notice.style.cssText = 'padding:1.2em;text-align:center;color:#777;';
        notice.textContent = 'このページには絞り込み条件に一致する記事がありません。他のページもご確認ください。';
        listContainer.insertBefore(notice, listContainer.firstChild);
      }
    } else if (existingNotice) {
      existingNotice.remove();
    }

    // 【2026-08-29追記：フィルター適用中はページネーションを非表示にする】
    // 購入済み/スキ絞り込みはクライアント側限定で、サーバー側の全件数に
    // 基づくページネーション（「全35ページ」等）とは対応関係が無い。
    // 絞り込み中にこれを表示したままだと、2ページ目以降に遷移した読者が
    // 「該当記事なし」の空ページに迷い込む（上のnoticeで案内はしているが、
    // それ以前にページ送りへ誘導すること自体を避けたい）。フィルターが
    // 1つでも有効な間はページネーションのラッパー要素ごと非表示にし、
    // 全て解除されたら通常表示に戻す。
    document.querySelectorAll('.pagination, .pagination-next, .pagination-prev').forEach(function (el) {
      el.style.display = (wantPurchased || wantLiked) ? 'none' : '';
    });
  }

  // pageshowでの再実行時にリスナーが二重登録されないよう、bound済み
  // フラグで一度だけ登録する（他のクリックリスナーと同じ方式）。
  function bindMetaFilterCheckboxes() {
    ['nseb-filter-purchased', 'nseb-filter-liked'].forEach(function (id) {
      var cb = document.getElementById(id);
      if (cb && !cb.dataset.nsebBound) {
        cb.dataset.nsebBound = '1';
        cb.addEventListener('change', applyMetaFilters);
      }
    });
  }

  function initCardBadges() {
    var articles = document.querySelectorAll('article[id^="post-"]');
    if (!articles.length) { return; }

    var ids = [];
    var idToMetaEl = {};
    articles.forEach(function (art) {
      var m = art.id.match(/^post-(\d+)$/);
      if (!m) { return; }
      var id = m[1];
      var metaEl = art.querySelector('.entry-card-info, .entry-card-meta, .card-meta');
      if (!metaEl) { return; }
      ids.push(id);
      idToMetaEl[id] = metaEl;
    });
    if (!ids.length) { return; }

    bindMetaFilterCheckboxes();

    // ステップ1: LocalStorageを最優先で参照し、ネットワーク応答を待たずに
    // 即座にハートの見た目だけ書き換える（bfcache復元直後でも一覧に既存の
    // バッジがあればここで赤く点灯する）。
    paintCardLikeFromCache(idToMetaEl);

    // ステップ2: バッチAPI（1リクエストで全記事分。N+1を回避）を叩いて
    // 最新の「いいね」数・PV等を取得し、DOMを動的に書き換える
    // （PHPの静的出力キャッシュを完全にバイパスする）。
    fetchCardData(ids).then(function (data) {
      if (!data) { return; }
      renderCardBadges(idToMetaEl, data);
      // バッジの最終状態が確定した直後に、既にチェック済みの絞り込みが
      // あれば反映する（チェックボックスの状態がページ表示前から
      // 残っている場合でも、フェッチ完了を待たずに一覧全件を再表示した
      // ままにしない）。
      applyMetaFilters();
    });
  }

  function refreshAll() {
    initStatusBar();
    initCardBadges();
  }

  // DOMを触らないため、DOMContentLoadedを待たずページ読み込み直後に判定する。
  checkSubscriptionConversionParam();
  ready(refreshAll);

  // 「戻る」でbfcacheから復元された場合（persisted === true）は
  // DOMContentLoadedが発火しないため、pageshowで明示的に再実行する。
  // 【persistedで必ずガードする】通常のフルロードでもpageshowは発火するため、
  // ここを無条件に呼ぶとready()と合わせて毎回のページ表示のたびに
  // fetchCardData〜DOM書き換えが2重に走ってしまう（ネットワーク要求が
  // 無駄に倍になるだけでなく、バッジ要素が2回続けて作り直されることで
  // 描画タイミングの競合を引き起こすことを実機で確認した）。bfcache復元時
  // （persisted === true）に限定して再実行することで、通常ロードでは1回、
  // 戻る操作でも1回だけ、常に「1回だけ」再描画が走るようにする。
  window.addEventListener('pageshow', function (event) {
    if (event.persisted) {
      refreshAll();
    }
  });
})();
</script>
        <?php
    }

    /**
     * 単一記事ページのステータスバーの"骨組み"のみを出力する。
     * 価格・PV・スキ数の実際の値は一切ここに含めない（スケルトン表示のまま
     * JSに描画を委ねる）。the_titleフィルタの都合上、この時点ではまだ<h1>の
     * 中に一時的に挿入されており、正しい位置（<h1>の直後）へはJS側
     * （nseb-frontend.js）が移動させる。
     *
     * data-paid のみ例外的に実際の値（codoc_entry_codeの有無）を焼き込む。
     * 【2026-08-27追記】サブスク加入者一括判定（ACTIVE_SUBSCRIBER_KEY）による
     * 「🛒 購入済み」の即時（ネットワーク応答を待たない）楽観的描画が、無料記事
     * （Codocエントリー自体が無い記事）にまで誤って表示されてしまう不具合が
     * あったため、この判定にはfetch応答を待つ必要がある「価格そのもの」ではなく、
     * 単なる真偽値（有料記事かどうか）だけをスケルトン生成時点で焼き込む。
     */
    private function build_status_bar_skeleton_html($post_id) {
        $is_paid_article = (bool) get_post_meta($post_id, 'codoc_entry_code', true);
        ob_start();
        ?>
<div class="nseb-status-bar" data-post-id="<?php echo esc_attr($post_id); ?>" data-paid="<?php echo $is_paid_article ? '1' : '0'; ?>">
  <span class="nseb-stat nseb-stat-purchased nseb-skeleton" hidden style="display:none">…</span>
  <span class="nseb-stat nseb-price nseb-skeleton">…</span>
  <span class="nseb-stat nseb-stat-views"><span class="nseb-stat-icon">👁</span><span class="nseb-view-count nseb-skeleton">…</span></span>
  <button type="button" class="nseb-stat nseb-like-btn" data-post-id="<?php echo esc_attr($post_id); ?>">
    <span class="nseb-heart nseb-stat-icon">♡</span><span class="nseb-like-count nseb-skeleton">…</span>
  </button>
</div>
        <?php
        return ob_get_clean();
    }

    /**
     * 投稿保存時、手動の抜粋(post_excerpt)が未設定かつ本文冒頭に赤い案内枠がある場合、
     * 案内枠を除いた本文から自動で抜粋を生成して保存する。
     *
     * 【背景】Cocoonテーマの一覧カード概要（.entry-card-snippet）はWordPress標準の
     * get_the_excerptフィルタを経由せず独自ロジックで生成されているため
     * （実機で get_the_excerpt フィルタに検証用の目印文字列を追加しても一覧に
     * 反映されないことを確認済み）、フィルタでの介入ができない。そのため
     * post_excerpt（手動抜粋）を直接生成・保存し、テーマ側にそちらを優先させる方式にした。
     */
    public function auto_generate_excerpt_on_save($post_id, $post) {
        if (wp_is_post_autosave($post_id) || wp_is_post_revision($post_id)) {
            return;
        }
        if ($post->post_type !== 'post' || $post->post_status !== 'publish') {
            return;
        }
        if (!empty($post->post_excerpt)) {
            return; // 手動抜粋が既にある場合は上書きしない
        }

        // note記事はフルタイトル（note_full_title）をそのまま概要として使う。
        // 無ければ従来通り本文冒頭からの自動抜粋にフォールバックする。
        $full_title = get_post_meta($post_id, 'note_full_title', true);
        $excerpt = ($full_title !== '' && $full_title !== false)
            ? $full_title
            : $this->build_clean_excerpt($post->post_content);
        if (empty($excerpt)) {
            return;
        }

        remove_action('save_post', array($this, 'auto_generate_excerpt_on_save'));
        wp_update_post(array('ID' => $post_id, 'post_excerpt' => $excerpt));
        add_action('save_post', array($this, 'auto_generate_excerpt_on_save'), 10, 2);
    }

    /**
     * 本文から「赤い案内枠」と「目次（nav要素・目次案内の定型文）」を取り除いた
     * 上で、残った本文の純粋な冒頭テキストから抜粋を生成する。
     * 変更が全く無かった場合（案内枠も目次も無い記事）は空文字を返す
     * （＝呼び出し側でpost_excerptの上書きをスキップする）。
     */
    private function build_clean_excerpt($content) {
        $cleaned = $this->strip_upsell_box($content);
        $cleaned = $this->strip_toc($cleaned);
        if ($cleaned === $content) {
            return '';
        }

        $text = wp_strip_all_tags(strip_shortcodes($cleaned));
        $text = trim(preg_replace('/\s+/u', ' ', $text));
        if ($text === '') {
            return '';
        }
        return wp_trim_words($text, 55, ' […]');
    }

    /**
     * 目次のnav要素（<nav class="toc">またはnote由来の<nav class="o-tableOfContents">）と、
     * 直前にある「⬇️目次の読みたい項目を...⬇️」定型文の段落を取り除く。
     * 目次の各リンクは見出しの文言そのものなので、除去しないと抜粋が
     * 「目次の項目名の羅列」になってしまう。
     */
    private function strip_toc($content) {
        if (strpos($content, '<nav') === false) {
            return $content;
        }
        if (!class_exists('DOMDocument')) {
            return $content;
        }

        $prev_libxml = libxml_use_internal_errors(true);
        $doc = new DOMDocument();
        $doc->loadHTML(
            '<?xml encoding="utf-8" ?><div id="nseb-root">' . $content . '</div>',
            LIBXML_NOERROR | LIBXML_NOWARNING
        );
        libxml_clear_errors();
        libxml_use_internal_errors($prev_libxml);

        $xpath = new DOMXPath($doc);
        $navs = $xpath->query('//nav');
        if ($navs->length === 0) {
            return $content;
        }

        foreach (iterator_to_array($navs) as $nav) {
            // 目次navの直前にある「⬇️目次の...⬇️」案内文の段落も一緒に削除する。
            // note.com由来のHTMLはnavが兄弟要素ではなく別の<p>の中に
            // 入れ子になっている場合があるため、兄弟だけでなく文書順で
            // navより前にある要素全体（preceding軸）から探す。
            $prevMatches = $xpath->query(
                'preceding::*[self::p or self::div or self::li][contains(., "目次の読みたい項目")]',
                $nav
            );
            if ($prevMatches->length > 0) {
                $prev_to_remove = $prevMatches->item($prevMatches->length - 1);
                if ($prev_to_remove->parentNode !== null) {
                    $prev_to_remove->parentNode->removeChild($prev_to_remove);
                }
            }
            $nav->parentNode->removeChild($nav);
        }

        $root = $doc->getElementById('nseb-root');
        if ($root === null) {
            return $content;
        }
        $html = '';
        foreach ($root->childNodes as $child) {
            $html .= $doc->saveHTML($child);
        }
        return $html;
    }

    /**
     * 本文冒頭に挿入された赤い案内枠（<div style="...ff7b7b...">...サブスクボタン...</div>）
     * を検出して取り除く。案内枠が無ければ引数をそのまま返す。
     */
    private function strip_upsell_box($content) {
        $button_marker = self::CONTENT_SUBSCRIPTION_DOM_ID . '" class="codoc-subscriptions"></div></div>';
        $marker_pos = strpos($content, $button_marker);
        if ($marker_pos === false) {
            return $content;
        }
        $close_pos = strpos($content, '</div>', $marker_pos + strlen($button_marker));
        if ($close_pos === false) {
            return $content;
        }
        return substr($content, $close_pos + strlen('</div>'));
    }

    /**
     * 記事本文中の赤い案内枠には既にサブスク登録ボタン（Codocウィジェットと同一の
     * subscriptions要素）を直接埋め込んでいるため、単一記事ページに限り、
     * サイドバーの同ウィジェットは重複表示を避けるために非表示にする
     * （Codoc側の仕様上、同一subscriptionコードの要素は1つしかマウントされないため、
     * 表示のみ本文側を優先させる）。
     */
    public function maybe_hide_sidebar_subscription_widget($instance, $widget, $args) {
        if (is_single() && get_post_type() === 'post' && isset($args['widget_id']) && $args['widget_id'] === self::SIDEBAR_SUBSCRIPTION_WIDGET_ID) {
            return false;
        }
        return $instance;
    }

    /**
     * 2026-08-23 追記：note記事（公開から90日以上経過したアーカイブ）は
     * auto_sync_blogs.py の sync_codoc_discount により、当サイト限定で
     * 元値>100円だったものは100円まで値下げ済みになる。この既存の値下げに
     * 読者が気づけるよう、本文冒頭にアピールボックスを挿入する。
     *
     * 【2026-08-24 追記：表示条件の厳格化】当初は現在価格が100円であれば
     * （値下げ履歴の有無に関わらず）表示していたが、これだと「最初から100円で
     * 販売されている記事」（実際には値下げされていない）にまで「特別価格」を
     * 謳ってしまい事実と異なる、との指摘を受けて修正した。実際に値下げされた
     * ことが確認できる記事（sync_codoc_discount実行時に元価格をPRICE_BEFORE_DISCOUNT_KEY
     * へ保存済み、かつその元価格が100円を超えている＝実際に値下げが発生した）
     * にのみ表示するようにし、それ以外（値下げ履歴が無い・元から100円以下
     * だった記事）は非表示にする。表示価格自体は引き続き CODOC_CACHE_PRICE_KEY
     * （本文embedded codoc-blockのpriceをライブ更新で反映したキャッシュ値。
     * [[section 6のprice source of truthルール]]と同じ値）から動的に読む。
     */
    public function prepend_archive_discount_box($content) {
        if (is_admin() || !is_single() || !in_the_loop() || !is_main_query()) {
            return $content;
        }
        global $post;
        if (!$post || get_post_type($post) !== 'post' || !has_category(self::NOTE_CATEGORY_ID, $post)) {
            return $content;
        }
        $entry_code = get_post_meta($post->ID, 'codoc_entry_code', true);
        if (!$entry_code) {
            return $content; // Codocの有料エントリーが無い（無料）記事は対象外
        }
        $post_timestamp = get_post_time('U', false, $post);
        if (!$post_timestamp || (current_time('timestamp') - $post_timestamp) < self::ARCHIVE_DISCOUNT_AFTER_DAYS * DAY_IN_SECONDS) {
            return $content;
        }
        $current_price = (int) get_post_meta($post->ID, self::CODOC_CACHE_PRICE_KEY, true);
        if ($current_price !== 100) {
            return $content; // 値下げ後の特別価格（100円）ちょうどでなければ対象外
        }
        $before_price = (int) get_post_meta($post->ID, self::PRICE_BEFORE_DISCOUNT_KEY, true);
        if ($before_price <= 100) {
            return $content; // 値下げ履歴が無い、または元から100円以下だった記事は対象外
        }

        $price_line = sprintf(
            '本記事は公開から3ヶ月以上経過したアーカイブのため、<strong>当サイト限定の特別価格【%d円】</strong>（note定価%d円から%d円引き）でお読みいただけます。',
            $current_price, $before_price, $before_price - $current_price
        );

        $box = '<div class="archive-discount-box" style="background-color: #fdfbf7; border: 2px solid #e6b422; border-radius: 8px; padding: 15px; margin-bottom: 25px;">'
            . '<p style="margin: 0 0 10px; font-weight: bold; font-size: 1.1em; color: #d32f2f;">💡 この記事は当サイトで買うのが一番お得です！</p>'
            . '<p style="margin: 0 0 10px; font-size: 0.95em; line-height: 1.6;">'
            . $price_line
            . '<br>※そのまま下へスクロールし、記事内の購入ボタンからお進みください。'
            . '</p>'
            . '<p style="margin: 0; font-size: 0.85em; color: #666;">'
            . '※noteアカウント側に購入履歴を保存したい方のみ、記事内のnote元リンクをご利用ください。'
            . '</p>'
            . '</div>';

        return $box . $content;
    }
}

new Note_Style_Engagement_Bar();
