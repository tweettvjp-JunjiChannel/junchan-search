<?php
/**
 * Plugin Name: Custom Search Category Filter
 * Description: 検索結果をカテゴリで絞り込むフィルタと、note記事のフルタイトル（カスタムフィールド note_full_title）を検索対象に含めるカスタムフィールド優先検索を提供する。
 * Version: 1.3.0
 * Author: junchan-world
 */

if (!defined('ABSPATH')) {
    exit;
}

// note記事のフルタイトル（post_titleは95文字+"..."に切り詰められているため、
// note.com側の正式なタイトル全文をここに保持する）をREST API経由で読み書き
// できるように登録する。
add_action('init', function () {
    register_post_meta('post', 'note_full_title', array(
        'type'          => 'string',
        'single'        => true,
        'show_in_rest'  => true,
        'auth_callback' => function () {
            return current_user_can('edit_posts');
        },
    ));
});

class Custom_Search_Category_Filter {

    // チェックボックスのvalue => WordPressカテゴリID の対応表
    const CAT_MAP = array(
        'note'             => 2471,
        'exblog'           => 2445,
        'tweettv_scenario' => 2457,
        'deleted_tweet'    => 2456,
    );

    // チェックボックスが一切送信されなかった場合（フォーム未経由の直接URLアクセス等）のデフォルト
    const DEFAULT_CHECKED = array('note', 'exblog');

    const FULL_TITLE_META_KEY = 'note_full_title';
    const FULL_TITLE_JOIN_ALIAS = 'note_full_title_meta';

    public function __construct() {
        add_action('pre_get_posts', array($this, 'filter_search_query'));
        add_action('pre_get_posts', array($this, 'filter_front_page_query'));
        add_action('template_redirect', array($this, 'redirect_legacy_category_archive'));
        add_filter('posts_join', array($this, 'join_full_title_meta'), 10, 2);
        add_filter('posts_search', array($this, 'extend_search_to_full_title'), 10, 2);
        add_filter('posts_distinct', array($this, 'force_distinct_on_search'), 10, 2);
        add_filter('posts_orderby', array($this, 'prioritize_title_matches'), 10, 2);
    }

    /**
     * 【2026-08-16 追記：SEO・UX対応】Google検索結果等から、トップページでは
     * 既にnote・exblog以外を除外している古いカテゴリー（「ニュース」や
     * 「TweetTV」関連カテゴリー等）のアーカイブページ（/category/xxx/）へ
     * 直接アクセスしてくる訪問者がいる。トップページはfilter_front_page_query()
     * で絞り込み済みだが、カテゴリーアーカイブページ自体は素通しだったため、
     * そこを直接叩かれると古い記事群がそのまま見えてしまっていた。
     * 「表示を許可するカテゴリー（note・exblog）」をDEFAULT_CHECKED（＝
     * filter_front_page_queryと同じ単一の情報源）から動的に導出し、それ以外の
     * カテゴリーアーカイブへのアクセスは全てトップページへ301リダイレクトする。
     * 新しいカテゴリーが増えてもここを個別に追記する必要がないよう、
     * 「除外リストの列挙」ではなく「許可リストにあるかどうか」で判定する設計。
     */
    public function redirect_legacy_category_archive() {
        if (is_admin() || !is_category() || !is_main_query()) {
            return;
        }

        $queried = get_queried_object();
        if (!($queried instanceof WP_Term)) {
            return;
        }

        $visible_cat_ids = array();
        foreach (self::DEFAULT_CHECKED as $key) {
            if (isset(self::CAT_MAP[$key])) {
                $visible_cat_ids[] = self::CAT_MAP[$key];
            }
        }

        if (in_array((int) $queried->term_id, $visible_cat_ids, true)) {
            return;
        }

        wp_safe_redirect(home_url('/'), 301);
        exit;
    }

    /**
     * 【2026-08-14 追記：新規訪問者が古いTweetTV記事（2015年以前等）を
     * 大量に含む未絞り込みの一覧を見てしまう不具合の対応】
     * サイドバーの絞り込みフォーム（filter_search_query、DEFAULT_CHECKED=
     * note+exblog）は、検索結果ページ（is_search()）にしか適用されておらず、
     * トップページ（フロントページ＝ブログの投稿一覧、is_home()）の
     * メインクエリは一切絞り込まれていなかった。この状態はサイト開設時から
     * 一貫してそうだった（「以前は正しく絞られていたのに壊れた」という
     * 回帰ではなく、そもそも実装されていなかった機能欠落）。
     * トップページはLocalStorageもCookieも読めないサーバーサイドの
     * 初回レンダリングであるため、クライアント側の保存済み選択状態に
     * 依存させることはできない。ここではJSのlocalStorage連携とは独立して、
     * トップページのメインクエリを常にDEFAULT_CHECKED（note・exblog）へ
     * 固定的に絞り込む（新規・既存訪問者を問わず一律。他カテゴリーを
     * 見たい場合はサイドバーの絞り込みフォームで明示的に検索する導線を使う）。
     */
    public function filter_front_page_query($query) {
        if (is_admin() || !$query->is_home() || !$query->is_main_query()) {
            return;
        }

        $cat_ids = array();
        foreach (self::DEFAULT_CHECKED as $key) {
            if (isset(self::CAT_MAP[$key])) {
                $cat_ids[] = self::CAT_MAP[$key];
            }
        }
        if (!empty($cat_ids)) {
            $query->set('category__in', $cat_ids);
        }
    }

    /**
     * 検索キーワードがタイトルに含まれる記事を最優先（かつ新しい順）にする。
     * post_title（95文字に切り詰められている）だけでなく、note_full_title
     * メタ（note.com側の正式なタイトル全文）のいずれかに全ての検索語が
     * 含まれる記事を0、それ以外を1として並べ替え、同順位内は投稿日時の
     * 新しい順にする（WP標準の関連度スコアより明確で意図が分かりやすい
     * ため、標準のorderbyを丸ごと置き換える）。
     */
    public function prioritize_title_matches($orderby, $query) {
        global $wpdb;
        if (is_admin() || !$query->is_search() || !$query->is_main_query()) {
            return $orderby;
        }

        $terms = $query->get('search_terms');
        if (empty($terms)) {
            return $orderby;
        }

        $alias = self::FULL_TITLE_JOIN_ALIAS;
        $conditions = array();
        foreach ($terms as $term) {
            $like = '%' . $wpdb->esc_like($term) . '%';
            $conditions[] = $wpdb->prepare(
                "({$wpdb->posts}.post_title LIKE %s OR {$alias}.meta_value LIKE %s)",
                $like,
                $like
            );
        }
        $title_match_sql = implode(' AND ', $conditions);

        return "CASE WHEN ({$title_match_sql}) THEN 0 ELSE 1 END ASC, {$wpdb->posts}.post_date DESC";
    }

    public function filter_search_query($query) {
        if (is_admin() || !$query->is_search() || !$query->is_main_query()) {
            return;
        }

        if (isset($_GET['filter_submitted'])) {
            $raw = isset($_GET['filter_cats']) && is_array($_GET['filter_cats']) ? $_GET['filter_cats'] : array();
            $checked = array_map('sanitize_text_field', wp_unslash($raw));
        } else {
            $checked = self::DEFAULT_CHECKED;
        }

        $cat_ids = array();
        foreach ($checked as $key) {
            if (isset(self::CAT_MAP[$key])) {
                $cat_ids[] = self::CAT_MAP[$key];
            }
        }

        if (empty($cat_ids)) {
            // 全チェック解除で送信された場合は「該当なし」として0件を返す
            $query->set('post__in', array(0));
        } else {
            $query->set('category__in', $cat_ids);
        }
    }

    /**
     * 検索クエリに note_full_title メタ値を比較できるよう LEFT JOIN を追加する
     * （メタが存在しない投稿も除外されないよう INNER JOIN ではなく LEFT JOIN を使う）。
     */
    public function join_full_title_meta($join, $query) {
        global $wpdb;
        if (is_admin() || !$query->is_search() || !$query->is_main_query()) {
            return $join;
        }
        $alias = self::FULL_TITLE_JOIN_ALIAS;
        if (strpos($join, $alias) === false) {
            $join .= " LEFT JOIN {$wpdb->postmeta} AS {$alias} ON ({$wpdb->posts}.ID = {$alias}.post_id AND {$alias}.meta_key = '" . esc_sql(self::FULL_TITLE_META_KEY) . "') ";
        }
        return $join;
    }

    /**
     * 標準の検索条件（タイトル・本文の LIKE 検索）に、note_full_title メタ値の
     * LIKE 検索を OR で追加する（カスタムフィールド優先検索）。
     * WP_Query::parse_search() が生成する $search は必ず " AND (...)" の形式で
     * 始まるため、先頭の " AND " を取り除いた中身を OR で括り直す。
     */
    public function extend_search_to_full_title($search, $query) {
        global $wpdb;
        if (is_admin() || !$query->is_search() || !$query->is_main_query() || empty($search)) {
            return $search;
        }

        $terms = $query->get('search_terms');
        if (empty($terms)) {
            return $search;
        }

        $alias = self::FULL_TITLE_JOIN_ALIAS;
        $meta_likes = array();
        foreach ($terms as $term) {
            $like = '%' . $wpdb->esc_like($term) . '%';
            $meta_likes[] = $wpdb->prepare("{$alias}.meta_value LIKE %s", $like);
        }
        $meta_clause = '(' . implode(' AND ', $meta_likes) . ')';

        if (strpos($search, ' AND (') === 0) {
            $inner = substr($search, strlen(' AND '));
            // $inner は "(...)" の形式なので、そのまま OR で meta_clause と結合する
            return " AND ( {$inner} OR {$meta_clause} )";
        }

        return $search;
    }

    /**
     * LEFT JOINにより行が重複しないよう、検索メインクエリでは DISTINCT を強制する。
     */
    public function force_distinct_on_search($distinct, $query) {
        if (!is_admin() && $query->is_search() && $query->is_main_query()) {
            return 'DISTINCT';
        }
        return $distinct;
    }
}

new Custom_Search_Category_Filter();
