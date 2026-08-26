<?php
/**
 * Plugin Name: Custom Search Category Filter
 * Description: 検索結果をカテゴリで絞り込むフィルタと、note記事のフルタイトル（カスタムフィールド note_full_title）を検索対象に含めるカスタムフィールド優先検索を提供する。
 * Version: 1.7.0
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

    // 「ニュース」カテゴリー（2026-08-16に復活）。トップページのデフォルト
    // 絞り込み（DEFAULT_CHECKED）には含めないが、アーカイブページ単体は
    // 生きたページとして許可するため、リダイレクト判定でのみ個別に許可する。
    const NEWS_CATEGORY_ID = 2448;

    const FULL_TITLE_META_KEY = 'note_full_title';
    const FULL_TITLE_JOIN_ALIAS = 'note_full_title_meta';

    // 【2026-08-24 追記】TweetTVの移行時に作成された一部の運用・テンプレート用
    // 投稿（「緊急重要リンク」「ヘッダーログ」「テンプレート」カテゴリー等）に、
    // 実際の公開日が不明なまま1911年・1912年という誤ったpost_dateが設定された
    // ままになっており、サイドバーの月別アーカイブ一覧（wp_get_archives）に
    // ありえない年月として表示されてしまっていた。正しい公開日が判明していない
    // 以上、日付を推測で書き換える（正常化）のではなく、アーカイブ一覧の生成
    // クエリ側でこの現実的でない期間を除外する方式を採用する。
    const ARCHIVE_MIN_DATE = '2000-01-01 00:00:00';

    // 【2026-09-02追記】「マイ本棚」機能で使うクエリ変数・タブ名の定数。
    const MY_LIBRARY_VIEW = 'my-library';
    const MY_LIBRARY_TABS = array('purchased', 'liked', 'both');

    public function __construct() {
        add_action('pre_get_posts', array($this, 'filter_search_query'));
        add_action('pre_get_posts', array($this, 'filter_front_page_query'));
        // 【2026-09-02追記：「マイ本棚」機能】購入した記事/スキした記事/
        // 両方(AND)をpost__inで正しく全期間から抽出する専用ビュー
        // （?view=my-library）。filter_front_page_queryより後に登録し、
        // かつfilter_front_page_query側にも早期returnガードを追加すること
        // で、is_home()扱いになるこのビューにcategory__inが二重に
        // 適用されないようにしている。
        add_action('pre_get_posts', array($this, 'filter_my_library_query'));
        add_action('loop_start', array($this, 'render_my_library_tabs'));
        add_action('template_redirect', array($this, 'redirect_legacy_category_archive'));
        add_filter('posts_join', array($this, 'join_full_title_meta'), 10, 2);
        add_filter('posts_search', array($this, 'extend_search_to_full_title'), 10, 2);
        add_filter('posts_distinct', array($this, 'force_distinct_on_search'), 10, 2);
        add_filter('posts_orderby', array($this, 'prioritize_title_matches'), 10, 2);
        // サイドバー等の月別/年別アーカイブ一覧から、ARCHIVE_MIN_DATE より前の
        // （実際には存在しないはずの）年月を除外する。
        add_filter('getarchives_where', array($this, 'filter_archives_where'));

        // 【2026-08-16 追記：「ニュース」カテゴリー復活・自動分類】
        // save_postはREST経由の新規作成時、note_full_titleメタが未反映の
        // 段階（wp_insert_post直後）で発火してしまうため、post_title単体の
        // 判定用フォールバックとして残しつつ、メタの実書き込みタイミングを
        // 正確に捉えられる added/updated_post_meta（note_full_titleキーのみ）
        // もあわせてフックし、どちらの経路でも取りこぼさないようにする。
        add_action('save_post', array($this, 'handle_save_post'), 20, 3);
        add_action('added_post_meta', array($this, 'handle_note_full_title_meta_change'), 10, 4);
        add_action('updated_post_meta', array($this, 'handle_note_full_title_meta_change'), 10, 4);
    }

    /**
     * タイトル末尾が「…8/12」「...８／１２」のように、省略記号（"…"または"..."）
     * に続けて「月/日」形式の日付で終わっているかを判定する。全角/半角の
     * 数字・スラッシュ・空白混在に対応する。backfill_news_category.py の
     * NEWS_TITLE_PATTERN と完全に同じロジック（変更する場合は両方を直すこと）。
     */
    public static function title_looks_like_news($title) {
        $title = trim((string) $title);
        if ($title === '') {
            return false;
        }
        return (bool) preg_match('/(?:\.\.\.|…)\s*[0-90-9]{1,2}\s*[\/／]\s*[0-90-9]{1,2}\s*$/u', $title);
    }

    /**
     * 対象記事に「ニュース」カテゴリーを追加する（既存カテゴリーは維持。
     * wp_set_post_categoriesの第3引数trueで「置き換え」ではなく「追記」にする）。
     * 判定材料は post_title と note_full_title の両方をORで見る
     * （どちらか一方でもパターンに合致すれば付与する。優先順位を付けず
     * 両方チェックすることで、save_postとメタ変更フックのどちらが先に
     * 走っても取りこぼさない）。
     */
    private function maybe_assign_news_category($post_id) {
        $post = get_post($post_id);
        if (!$post || $post->post_type !== 'post') {
            return;
        }
        if (in_array($post->post_status, array('auto-draft', 'trash'), true)) {
            return;
        }

        $candidate_titles = array(
            (string) $post->post_title,
            (string) get_post_meta($post_id, self::FULL_TITLE_META_KEY, true),
        );

        $matched = false;
        foreach ($candidate_titles as $t) {
            if (self::title_looks_like_news($t)) {
                $matched = true;
                break;
            }
        }
        if (!$matched) {
            return;
        }

        $current = wp_get_post_categories($post_id);
        if (in_array(self::NEWS_CATEGORY_ID, $current, true)) {
            return;
        }

        wp_set_post_categories($post_id, array_merge($current, array(self::NEWS_CATEGORY_ID)), true);
    }

    public function handle_save_post($post_id, $post, $update) {
        if (wp_is_post_revision($post_id) || wp_is_post_autosave($post_id)) {
            return;
        }
        $this->maybe_assign_news_category($post_id);
    }

    /**
     * note_full_titleメタの書き込み（追加・更新）を捉える。update_post_meta()/
     * add_post_meta()はDBへの書き込み完了後にこのアクションを発火するため、
     * save_postと違いここでは新しいメタ値を確実に読める（REST APIでの新規
     * 投稿作成時、save_postはwp_insert_post直後＝メタ反映前に発火してしまう
     * ため、この経路が実質的な主な入口になる）。
     */
    public function handle_note_full_title_meta_change($meta_id, $object_id, $meta_key, $meta_value) {
        if ($meta_key !== self::FULL_TITLE_META_KEY) {
            return;
        }
        $this->maybe_assign_news_category($object_id);
    }

    /**
     * 【2026-08-16 追記：SEO・UX対応】Google検索結果等から、トップページでは
     * 既にnote・exblog以外を除外している古いカテゴリー（「TweetTV」関連
     * カテゴリー等）のアーカイブページ（/category/xxx/）へ直接アクセスして
     * くる訪問者がいる。トップページはfilter_front_page_query()で絞り込み
     * 済みだが、カテゴリーアーカイブページ自体は素通しだったため、そこを
     * 直接叩かれると古い記事群がそのまま見えてしまっていた。
     * 「表示を許可するカテゴリー（note・exblog）」をDEFAULT_CHECKED（＝
     * filter_front_page_queryと同じ単一の情報源）から動的に導出し、それ以外の
     * カテゴリーアーカイブへのアクセスは全てトップページへ301リダイレクトする。
     * 新しいカテゴリーが増えてもここを個別に追記する必要がないよう、
     * 「除外リストの列挙」ではなく「許可リストにあるかどうか」で判定する設計。
     *
     * 【同日追記】「ニュース」カテゴリーは、末尾が「…M/D」形式で終わるnote
     * 記事を自動分類する生きたアーカイブとして復活させたため、ここでは
     * リダイレクト対象から個別に除外する。トップページのデフォルト絞り込み
     * （DEFAULT_CHECKED）には含めない＝トップには出さないが、アーカイブ
     * ページ単体へのアクセスは許可する、という非対称な扱いのため、
     * DEFAULT_CHECKEDそのものを変更するのではなくNEWS_CATEGORY_IDをここだけ
     * 個別に許可リストへ足す。
     */
    public function redirect_legacy_category_archive() {
        if (is_admin() || !is_category() || !is_main_query()) {
            return;
        }

        $queried = get_queried_object();
        if (!($queried instanceof WP_Term)) {
            return;
        }

        $visible_cat_ids = array(self::NEWS_CATEGORY_ID);
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
        // 「マイ本棚」（?view=my-library）はis_home()として扱われるが、
        // category__inではなくpost__in（filter_my_library_query）で
        // 絞り込むべきビューのため、ここでは何もしない。
        if (isset($_GET['view']) && $_GET['view'] === self::MY_LIBRARY_VIEW) {
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
     * 【2026-09-02追記：「マイ本棚」機能】前回の実装（検索フォームの
     * チェックボックス経由でliked_ids[]/purchased_ids[]をpost__inに渡す方式）
     * を、専用のビュー（?view=my-library&tab=purchased|liked|both）へ
     * 切り出した。検索フォームは通常検索（キーワード＋カテゴリ）に専念させる。
     *
     * タブごとの対象ID：
     *   purchased : purchased_ids[]（LocalStorageのnseb_purchased_posts）
     *   liked     : liked_ids[]（LocalStorageのnseb_liked_posts）
     *   both      : purchased_ids[] と liked_ids[] の共通要素（array_intersect）
     *
     * このビューはURLに s パラメータを持たない（is_home()扱い）ため、
     * filter_front_page_query() 側にも早期returnガードを追加し、
     * category__inによる絞り込みと二重適用にならないようにしている。
     * 該当0件の場合もpost__inを空配列のままにしない（空配列はWP_Queryに
     * 無視され「絞り込み無し」＝全件表示になってしまうため）、存在しない
     * ID(0)を明示的に指定して「該当なし」をWordPress標準の仕組みで
     * 正しく表現する。
     */
    public function filter_my_library_query($query) {
        if (is_admin() || !$query->is_main_query()) {
            return;
        }
        if (!isset($_GET['view']) || $_GET['view'] !== self::MY_LIBRARY_VIEW) {
            return;
        }

        $tab = isset($_GET['tab']) ? sanitize_text_field(wp_unslash($_GET['tab'])) : 'purchased';
        if (!in_array($tab, self::MY_LIBRARY_TABS, true)) {
            $tab = 'purchased';
        }

        $purchased_ids = (isset($_GET['purchased_ids']) && is_array($_GET['purchased_ids']))
            ? array_map('absint', wp_unslash($_GET['purchased_ids'])) : array();
        $liked_ids = (isset($_GET['liked_ids']) && is_array($_GET['liked_ids']))
            ? array_map('absint', wp_unslash($_GET['liked_ids'])) : array();

        switch ($tab) {
            case 'liked':
                $ids = $liked_ids;
                break;
            case 'both':
                $ids = array_values(array_intersect($purchased_ids, $liked_ids));
                break;
            case 'purchased':
            default:
                $ids = $purchased_ids;
                break;
        }
        $ids = array_values(array_unique(array_filter($ids)));

        $query->set('post_type', 'post');
        $query->set('post__in', empty($ids) ? array(0) : $ids);
    }

    /**
     * 「マイ本棚」ビュー（?view=my-library）の一覧上部に、3つの切り替え
     * タブ（購入した記事/スキした記事/購入&スキ）を出力する。実際の
     * href（?purchased_ids[]=.../liked_ids[]=...）は、LocalStorageの現在の
     * 記事ID一覧を読める側＝JS（note-style-engagement.php の
     * wireMyLibraryLinks）が後から書き換える。ここではdata-tab属性付きの
     * プレースホルダを出力するだけでよい。
     *
     * loop_start はメインループ以外（ウィジェット内のミニクエリ等）でも
     * 発火しうるため、$queryがメインクエリかどうかを確認し、かつ1リクエスト
     * につき1回だけ出力する。
     */
    public function render_my_library_tabs($query) {
        if (is_admin() || !$query->is_main_query()) {
            return;
        }
        if (!isset($_GET['view']) || $_GET['view'] !== self::MY_LIBRARY_VIEW) {
            return;
        }
        static $rendered = false;
        if ($rendered) {
            return;
        }
        $rendered = true;

        $current_tab = isset($_GET['tab']) ? sanitize_text_field(wp_unslash($_GET['tab'])) : 'purchased';
        if (!in_array($current_tab, self::MY_LIBRARY_TABS, true)) {
            $current_tab = 'purchased';
        }
        ?>
<div id="nseb-my-library-tabs" style="margin:0 0 1.5em;text-align:center;">
  <a href="#" data-tab="purchased" class="nseb-library-tab<?php echo $current_tab === 'purchased' ? ' is-active' : ''; ?>">🛒 購入した記事</a>
  <a href="#" data-tab="liked" class="nseb-library-tab<?php echo $current_tab === 'liked' ? ' is-active' : ''; ?>">♥ スキした記事</a>
  <a href="#" data-tab="both" class="nseb-library-tab<?php echo $current_tab === 'both' ? ' is-active' : ''; ?>">🌟 購入＆スキ（両方）</a>
</div>
        <?php
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

    /**
     * wp_get_archives()（サイドバー等の月別/年別アーカイブ一覧ウィジェット）の
     * SQL WHERE句に、ARCHIVE_MIN_DATE以降という条件を追加する。post_date自体は
     * 書き換えず、一覧表示からのみ除外することで、正しい公開日が不明な投稿の
     * 日付を推測で捏造することを避ける。
     */
    public function filter_archives_where($where) {
        global $wpdb;
        return $where . $wpdb->prepare(' AND post_date >= %s', self::ARCHIVE_MIN_DATE);
    }
}

new Custom_Search_Category_Filter();
