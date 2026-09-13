<?php
/**
 * Plugin Name: Custom Search Category Filter
 * Description: 検索結果をカテゴリで絞り込むフィルタと、note記事のフルタイトル（カスタムフィールド note_full_title）を検索対象に含めるカスタムフィールド優先検索を提供する。
 * Version: 1.25.0
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

    // 【2026-09-01追記、2026-09-02更新：テーマ別2階層カテゴリー】
    // categorize_articles_v2.py が作成する大カテゴリー名の一覧（9大分類×22小分類
    // 体系）。redirect_legacy_category_archive() で「これらの大カテゴリー、
    // またはその子カテゴリー」のアーカイブページは表示を許可する（トップページには
    // 出さないが、個別ページへの直接アクセス・サイドバーのアコーディオンからの
    // リンクは許可する、NEWS_CATEGORY_IDと同じ非対称な扱い）。命名は
    // categorize_articles_v2.pyのTAXONOMY定数と完全に一致させること
    // （分類ロジックを変更したら両方を必ず更新する）。
    // 【2026-09-02追記】旧29大カテゴリー（categorize_articles_ai.py、2026-09-01作成）
    // の名前も、過去に外部リンクされた可能性やSEOインデックス済みの可能性を考慮し、
    // 引き続き許可リストに残す（記事は既に新体系へ付け替え済みで空カテゴリーに
    // なっているが、直リンクされた場合に404/リダイレクトループにしないため）。
    const AI_TAXONOMY_MAJOR_NAMES = array(
        // 9大分類×22小分類（現行）
        '思考・倫理・精神構造の解体', '国内政治・権力監視', '国際政治・グローバルガバナンス',
        '歴史の深掘り・真相究明', '医療・健康・生命倫理', '宗教・思想・社会構造',
        '科学技術・環境・AI', '文化・エンターテインメントの裏側', 'その他',
        // 旧29大カテゴリー（2026-09-01作成、現在は空カテゴリー。直リンク保護用に残置）
        '米国政治・トランプ政権', '日本の政局', '皇室・天皇制の起源',
        'バチカン・キリスト教とイエズス会', '宗教団体・カルト', 'CIA・諜報機関の暗躍',
        'エプスタイン・性的搾取スキャンダル', 'ワクチン・医療問題', 'WHO・パンデミック',
        'イスラエル・中東情勢', 'イラン情勢', 'ウクライナ・ロシア情勢', '中国・東アジア情勢',
        'ベネズエラ・中南米情勢', 'AI・テクノロジー', '金融・グローバリスト',
        '実業家・著名人の暗躍', '芸能界の闇', 'スポーツ', '都市伝説・オカルト・古代文明',
        '歴史の真相', '気象兵器・環境操作', '地震兵器・災害の人為性', '統治システム・国家の裏側',
        '事件・事故の真相', '社会問題・差別', 'ネット言論・プロパガンダ', '順ちゃん・番組関連',
        'その他・未分類テーマ',
    );

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

    // 【2026-09-02追記：一覧の並び替え】?sort=new|pv|like をトップページ・
    // カテゴリーアーカイブ・検索結果の共通クエリパラメータとして扱う。
    // pv/likeはpostmetaキャッシュ（note-style-engagement.php参照）で
    // ソートするため、meta_value_numのWP標準挙動（そのmetaを持たない投稿が
    // 結果から除外される）を避けるmeta_queryのEXISTS/NOT EXISTS OR技法を使う。
    const SORT_PARAM = 'sort';
    const SORT_META_KEYS = array(
        'pv' => 'nseb_pv_sort_cache',
        'like' => 'nseb_like_count',
    );

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
        add_action('pre_get_posts', array($this, 'apply_sort_param'));
        add_action('pre_get_posts', array($this, 'filter_tag_by_category'));
        add_action('loop_start', array($this, 'render_my_library_tabs'));
        add_action('loop_start', array($this, 'render_homepage_hero'));
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

        // 【2026-09-01追記】ツイキャス風ページャー（print_twitcasting_pager_assetsの
        // コメント参照）。サイト全体で読み込んで問題ない軽量なCSS/JSのみのため、
        // 出し分け条件は付けずwp_footerで常時出力する（該当する.paginationが
        // 無いページではJS側が何もしないだけで無害）。
        add_action('wp_footer', array($this, 'print_twitcasting_pager_assets'));
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

        // 【2026-09-01追記】自分自身、またはいずれかの祖先カテゴリーの名前が
        // AI_TAXONOMY_MAJOR_NAMESに含まれていれば、テーマ別2階層カテゴリー
        // （大カテゴリー自身、またはその子カテゴリー）のアーカイブとみなし表示を許可する。
        $check_term = $queried;
        for ($depth = 0; $depth < 5 && $check_term instanceof WP_Term; $depth++) {
            if (in_array($check_term->name, self::AI_TAXONOMY_MAJOR_NAMES, true)) {
                return;
            }
            if ((int) $check_term->parent === 0) {
                break;
            }
            $check_term = get_term($check_term->parent, 'category');
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
     * 【2026-09-03追記：トップページ・プレリリース版ヒーローセクション】
     * トップページ（is_home()、ページ1のみ）の記事一覧の直上に、4つの
     * 看板ブロック（①生放送告知、②横断検索ポータルへの導線、③YouTube
     * メンバーシップ、④Codoc月額読み放題＋アーカイブ紹介）を挿入する。
     *
     * render_my_library_tabs と同じ loop_start フックを使う（メインループの
     * 直前に確実に1回だけ挿入できる、既存のこのプラグインの実績パターン）。
     * is_paged()でページ1のみに限定し、?view=my-library（マイ本棚）とは
     * 完全に別ビューのため除外する（同じis_home()扱いだが趣旨が異なる）。
     *
     * 【グラウンディング】このヒーローセクションが参照するリンク・埋め込みは、
     * 実装前に全て実サイトへ実機確認済み：
     * - /links/ … 実在するページ（post-57578）。X検索・ツイキャス検索・
     *   YouTube検索を統合したポータルとして既に稼働中（jw-links-page）。
     * - https://twitcasting.tv/tweettvjp … 実在するツイキャスチャンネル。
     * - https://www.youtube.com/@ninja_truther/join … /links/ページの
     *   実際のYouTubeメンバーシップURLと同一。
     * - YouTube埋め込み（v=16w6vciEQ2g, list=PLKuq3LRJIIMM）… YouTube oEmbed
     *   APIで実際に存在・埋め込み可能であることを確認済み（チャンネル名
     *   「忍者トゥルーサー」が/links/ページのチャンネルと一致）。
     * - Codoc月額読み放題プランへの導線は、サイドバーの既存ウィジェット
     *   （#custom_html-2、Codoc公式cms.js埋め込み）を「二重埋め込み」せず、
     *   アンカーリンク（#custom_html-2 へスクロール）で誘導する設計にした。
     *   Codoc埋め込みは同一DOM IDの重複に弱く、過去に複数の不具合
     *   （CLAUDE.md 13〜15項）を起こしている経緯があるため、同じスクリプト・
     *   同じdiv idを2箇所に置く新規リスクを避けた。
     *
     * 【2026-09-10追記：「左記の」表現とサイドバー実位置の矛盾を修正】
     * 前回実装のCodoc誘導カードは「左記の『月額読み放題プラン』の
     * 『購入手続き』ボタンをクリックしてください」という文言＋
     * #custom_html-2へのアンカースクロールボタンを常時表示していたが、
     * 人間の実機目視で指摘を受け再調査した結果、このサイトのCocoonテーマは
     * 960px未満のビューポート（スマホ含む）では`.sidebar`自体を通常の
     * ドキュメントフロー上に一切描画しない（実機診断：is_visible()=False、
     * bounding_box=None）ことが判明した。スマホでは画面下部の固定ナビ
     * バー内「サイドバー」アイコンをタップして初めてオフキャンバス
     * パネルとして表示される仕組みのため、①「左記の」という表現は
     * スマホでは方向として意味を成さず、②アンカースクロールボタンも
     * 非表示要素をスクロール対象にするだけで実質何も起きない、という
     * 二重の実害があった。
     * 対策として、Cocoonの実際のブレークポイント（960px、本ファイル内の
     * `@media (min-width: 960px) { .sidebar{...} }` と一致させる）を境に、
     * デスクトップ版（「左記の」＋アンカースクロールボタン、実際に機能する）
     * とスマホ版（「画面下部のサイドバーアイコンをタップ」という、実際の
     * 操作手順に即した文言。ボタンは出さない＝押しても何も起きない偽の
     * ボタンを置かない）を、CSSの@mediaで排他的に出し分ける。
     */
    public function render_homepage_hero($query) {
        if (is_admin() || !$query->is_home() || !$query->is_main_query()) {
            return;
        }
        if (is_paged()) {
            return;
        }
        if (isset($_GET['view']) && $_GET['view'] === self::MY_LIBRARY_VIEW) {
            return;
        }
        static $rendered = false;
        if ($rendered) {
            return;
        }
        $rendered = true;
        ?>
<div class="jw-home-hero">
<style>
.jw-home-hero{max-width:900px;margin:0 auto 2em;}
.jw-hero-card{border-radius:14px;padding:1.4em 1.6em;margin-bottom:1.2em;box-shadow:0 1px 6px rgba(0,0,0,0.1);}
.jw-hero-card h2{margin:0 0 .5em;line-height:1.4;}
.jw-hero-card p{margin:0 0 1em;line-height:1.7;}
.jw-hero-btn{display:inline-flex;align-items:center;justify-content:center;min-height:48px;padding:.7em 1.4em;border-radius:8px;text-decoration:none;font-weight:bold;box-sizing:border-box;text-align:center;}
.jw-hero-btn:hover{opacity:.88;}
/* ① 生放送告知：最も目立つ赤系 */
.jw-hero-broadcast{background:linear-gradient(135deg,#c0392b,#e74c3c);color:#fff;}
.jw-hero-broadcast h2{font-size:1.5em;}
@media (max-width:480px){.jw-hero-broadcast h2{font-size:1.15em;}}
.jw-hero-broadcast p{font-size:1.05em;}
@media (max-width:480px){.jw-hero-broadcast p{font-size:.95em;}}
.jw-hero-broadcast .jw-hero-btn{background:#fff;color:#c0392b;font-size:1.05em;}
/* ② 横断検索ポータル */
.jw-hero-search{background:#f2f6fc;border:1px solid #dbe6f5;}
.jw-hero-search h2{font-size:1.25em;color:#1a4d8f;}
@media (max-width:480px){.jw-hero-search h2{font-size:1.05em;}}
.jw-hero-search p{font-size:1em;color:#333;}
@media (max-width:480px){.jw-hero-search p{font-size:.9em;}}
.jw-hero-search .jw-hero-btn{background:#1a4d8f;color:#fff;}
/* ③ YouTubeメンバーシップ */
.jw-hero-youtube{background:#fff5f5;border:1px solid #ffdddd;}
.jw-hero-youtube h2{font-size:1.2em;color:#cc0000;}
@media (max-width:480px){.jw-hero-youtube h2{font-size:1.05em;}}
.jw-hero-youtube .jw-hero-video-wrap{position:relative;width:100%;padding-top:56.25%;margin:0 0 1em;border-radius:8px;overflow:hidden;background:#000;}
.jw-hero-youtube .jw-hero-video-wrap iframe{position:absolute;top:0;left:0;width:100%;height:100%;border:0;}
.jw-hero-youtube .jw-hero-btn{background:#cc0000;color:#fff;}
/* ④ Codoc月額読み放題＋アーカイブ紹介 */
.jw-hero-codoc{background:#fff9ec;border:1px solid #f5e3b3;}
.jw-hero-codoc h2{font-size:1.2em;color:#8a6d00;}
@media (max-width:480px){.jw-hero-codoc h2{font-size:1.05em;}}
.jw-hero-codoc .jw-hero-btn{background:#8a6d00;color:#fff;}
.jw-hero-codoc-guide{font-weight:bold;padding:.8em 1em;border-radius:8px;background:#fff3cd;border:1px dashed #d4a017;font-size:1em;}
@media (max-width:480px){.jw-hero-codoc-guide{font-size:.92em;}}
/* 【2026-09-10追記】Cocoonの実ブレークポイント（960px）に合わせ、
   デスクトップ（サイドバーが左に常時表示、アンカースクロールが実際に
   機能する）とスマホ（サイドバーは画面下部ナビの「サイドバー」アイコンを
   タップするまで非表示＝アンカースクロールしても何も起きない）とで、
   文言とボタンの両方を排他的に出し分ける。同時に両方が見える状態は
   絶対に作らない（.jw-hero-codoc-guide-desktopはボタンにも付与する）。 */
@media (min-width:960px){.jw-hero-codoc-guide-mobile{display:none;}}
@media (max-width:959px){.jw-hero-codoc-guide-desktop{display:none;}}
</style>

<div class="jw-hero-card jw-hero-broadcast">
<h2>📡 毎週土曜よる8時〜 生放送「順チャンネル」<br>『ボーっと生きてんじゃねーよ！』ニュース</h2>
<p>10年以上続くニュース解説の生放送はツイキャスで毎週配信中！コメントでの参加・過去アーカイブの視聴もできます。</p>
<a class="jw-hero-btn" href="https://twitcasting.tv/tweettvjp" target="_blank" rel="noopener">🎙️ メンバーシップコミュニティに参加する</a>
</div>

<div class="jw-hero-card jw-hero-search">
<h2>🔍 動画・生中継・X発言を一括横断検索！</h2>
<p>10年以上・1,000本超の現場中継アーカイブと、2万人超フォロワーの公式X（旧Twitter）での日々の鋭い情報発信を、サイト内から一括で横断検索できます。</p>
<a class="jw-hero-btn" href="https://junchan-world.com/links/">🔎 動画・生中継・X発言を検索する</a>
</div>

<div class="jw-hero-card jw-hero-youtube">
<h2>🎬 AIが読み解く音声解説：NotebookLM解説動画</h2>
<div class="jw-hero-video-wrap">
<iframe src="https://www.youtube.com/embed/16w6vciEQ2g?list=PLKuq3LRJIIMM" title="NotebookLM解説動画プレイリスト" loading="lazy" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" allowfullscreen></iframe>
</div>
<p>解説動画・中継録画の全編はYouTubeメンバーシップで配信中です。</p>
<a class="jw-hero-btn" href="https://www.youtube.com/@ninja_truther/join" target="_blank" rel="noopener">▶️ YouTubeメンバーシップに加入する</a>
</div>

<div class="jw-hero-card jw-hero-codoc">
<h2>📚 削除された幻のテキストアーカイブも、ここに蘇る</h2>
<p>note等で一時公開・削除されてきた深層記事の数々を、Codoc月額読み放題プランで全文アーカイブ配信中。新着note記事は下の一覧からすぐにチェックできます。</p>
<p class="jw-hero-codoc-guide jw-hero-codoc-guide-desktop">👈 左記の「月額読み放題プラン」の「購入手続き」ボタンをクリックしてください</p>
<p class="jw-hero-codoc-guide jw-hero-codoc-guide-mobile">👇 画面下部のメニューから「サイドバー」をタップし、「月額読み放題プラン」の「購入手続き」ボタンをクリックしてください</p>
<a class="jw-hero-btn jw-hero-codoc-guide-desktop" href="#custom_html-2">💳 月額読み放題プランへ移動する</a>
</div>
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

    /**
     * 【2026-09-02追記】一覧の並び替え（新着順/PV順/スキ順）。トップページ・
     * カテゴリーアーカイブ・検索結果・「マイ本棚」以外のメインクエリに適用する。
     * new（デフォルト）は何もしない（WP標準のdate DESCのまま）。
     */
    public function apply_sort_param($query) {
        if (is_admin() || !$query->is_main_query()) {
            return;
        }
        // 【2026-09-02追記】サイドバーのタグアコーディオンから /tag/{slug}/ への
        // 絞り込みにも並び替えを適用できるよう is_tag() を追加。
        // 【2026-09-03追記】年代別アーカイブ（/YYYY/MM/）にも同様に is_date() を追加。
        if (!($query->is_home() || $query->is_category() || $query->is_tag() || $query->is_date() || $query->is_search())) {
            return;
        }
        if (isset($_GET['view']) && $_GET['view'] === self::MY_LIBRARY_VIEW) {
            return;
        }

        $sort = isset($_GET[self::SORT_PARAM]) ? sanitize_text_field(wp_unslash($_GET[self::SORT_PARAM])) : 'new';
        if (!isset(self::SORT_META_KEYS[$sort])) {
            return; // 'new' またはその他の未知の値はデフォルト（date DESC）のまま
        }

        $meta_key = self::SORT_META_KEYS[$sort];
        $query->set('meta_key', $meta_key);
        $query->set('orderby', 'meta_value_num');
        $query->set('order', 'DESC');
        // そのmetaキーを持たない投稿（一度も表示/スキされていない記事）が
        // 標準のmeta_value_numソートでは結果から除外されてしまうため、
        // EXISTS/NOT EXISTSのORでLEFT JOIN化し、全投稿を対象に含めたまま
        // 数値のある投稿を優先表示する（無い投稿はNULLとしてDESC末尾になる）。
        $query->set('meta_query', array(
            'relation' => 'OR',
            array('key' => $meta_key, 'compare' => 'EXISTS'),
            array('key' => $meta_key, 'compare' => 'NOT EXISTS'),
        ));
    }

    /**
     * 【2026-09-07追記：カテゴリ×タグ複合絞り込み（AND検索）】
     * サイドバーの小分類配下タグリンクは /tag/{tag_slug}/?cat={sub_category_id}
     * という形式で生成する（generate_category_sidebar_widget.py参照）。
     * ユーザーから「ワクチンカテゴリ下のトランプを押しても全カテゴリの
     * トランプ記事が出てくる」と報告され、実機検証の結果、以前はタグの
     * リンクが単なる /tag/{slug}/ でありカテゴリ情報を一切運んでいなかった
     * ことが真因と判明した。
     * WordPress標準の公開クエリ変数`cat`は、is_tag()コンテキストでWP_Queryへ
     * 渡すと自動的にカテゴリ側のtax_queryとAND結合されることを実機検証済み
     * （該当カテゴリ×タグの交差件数と、実際に一覧・ページングで取得できる
     * 件数が過不足なく一致することを確認：例 32件→10+10+10+2件×4ページ）。
     * 本メソッドは、この挙動が将来何らかの理由（他プラグイン・テーマ更新等）
     * で変化しても確実に同じ結果を保証するため、明示的にtax_queryを組み立てて
     * 上書きするセーフティネットとして追加する。
     * 【実装上の注意（実機で踏んだ罠）】`cat`が同時に指定されているとWP_Query
     * は is_tag() と is_category() の両方をtrueにするため、pre_get_posts内で
     * $query->get_queried_object() を呼ぶと「タグ」ではなく「カテゴリー」の
     * WP_Termが返ってくることがある（優先順位の都合）。これに気づかず
     * term_idをそのままpost_tagのtax_queryへ使ってしまい、
     * 存在しないタグID（実際はカテゴリーのterm_id）で検索してしまい
     * 「投稿が見つかりませんでした」という全滅の回帰を実機で確認した。
     * 対策として、queried_objectには頼らず、URLパスから来る`tag`クエリ変数
     * （タグのスラッグそのもの）を直接 get_term_by('slug', ..., 'post_tag')
     * で解決することで、この曖昧さを完全に回避する。
     */
    public function filter_tag_by_category($query) {
        if (is_admin() || !$query->is_tag() || !$query->is_main_query()) {
            return;
        }
        $cat_id = isset($_GET['cat']) ? absint($_GET['cat']) : 0;
        if ($cat_id <= 0) {
            return;
        }
        $tag_slug = $query->get('tag');
        if (empty($tag_slug)) {
            return;
        }
        $tag = get_term_by('slug', $tag_slug, 'post_tag');
        if (!$tag || is_wp_error($tag) || empty($tag->term_id)) {
            return;
        }
        $query->set('tax_query', array(
            'relation' => 'AND',
            array('taxonomy' => 'category', 'field' => 'term_id', 'terms' => array($cat_id)),
            array('taxonomy' => 'post_tag', 'field' => 'term_id', 'terms' => array($tag->term_id)),
        ));
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

    /**
     * 【2026-09-01追記：ツイキャス風ページャー】
     * Cocoonテーマが出力する標準のページネーション（`nav.pagination` /
     * `the_posts_pagination()`。WordPress core標準のmid_size/end_sizeによる
     * 省略記号付きページ番号）は、テーマファイル（cocoon-master）を直接
     * 書き換えないと挙動を変えられない。テーマ更新で上書きされるリスクを
     * 避けるため、既存のDOM（`.pagination` 内の `.page-numbers` リンク群。
     * 各ページ番号への実際のURLは既にWordPress側が正しく生成済み）を
     * 起点に、表示だけをJSでツイキャス（twitcasting.tv/tweettvjp/archive）
     * と同じ省略ルールに差し替える。実際の遷移先URLはWordPress生成のhrefを
     * そのまま使い回すため、リンク構造自体（SEO・クローラー向け）は変更しない。
     */
    public function print_twitcasting_pager_assets() {
        ?>
<style>
.tc-pager{display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:.3em;margin:1.5em 0;font-size:.95em;}
.tc-pager a.tc-pager-num,.tc-pager span.tc-pager-num{display:inline-flex;align-items:center;justify-content:center;min-width:2em;height:2em;padding:0 .4em;border-radius:4px;text-decoration:none;color:#333;box-sizing:border-box;}
.tc-pager a.tc-pager-num:hover{background:#eef3fb;}
.tc-pager .tc-pager-current{background:#3b7ddb;color:#fff;font-weight:bold;}
.tc-pager .tc-pager-dots{display:inline-flex;align-items:center;justify-content:center;min-width:1.5em;height:2em;color:#999;}
.tc-pager a.tc-pager-arrow{display:inline-flex;align-items:center;justify-content:center;min-width:2em;height:2em;text-decoration:none;color:#333;}
.tc-top-controls{margin:0 0 1em;}
.tc-top-controls .tc-pager{margin:.5em 0 1em;}
.tc-sort-tabs{display:flex;flex-wrap:wrap;gap:.5em;justify-content:center;margin:0 0 .3em;}
.tc-sort-tabs a.tc-sort-tab{display:inline-block;padding:.35em 1em;border-radius:999px;border:1px solid #ddd;text-decoration:none;color:#555;font-size:.9em;}
.tc-sort-tabs a.tc-sort-tab:hover{background:#f2f6fc;}
.tc-sort-tabs a.tc-sort-tab-active{background:#3b7ddb;border-color:#3b7ddb;color:#fff;font-weight:bold;}
/* 【2026-09-02追記：サイドバー独立スクロール】 .sidebar はデフォルトで
   position:staticのため、記事一覧側だけがどれだけ長くても常にページ全体と
   一緒にしか動けなかった。position:stickyにして自分自身もoverflow-y:autoで
   スクロール可能にすることで、ウィンドウをスクロールしてもサイドバーは
   画面内に留まり続け、かつサイドバー自体が長い（9大分類アコーディオン全開時等）
   場合は独立してスクロールできるようにする。*/
@media (min-width: 960px) {
  .sidebar{position:sticky;top:16px;max-height:calc(100vh - 32px);overflow-y:auto;overflow-anchor:none;}
}
/* 【2026-09-02追記：PJAX読み込み中オーバーレイ】#mainに相対配置で重ね、
   通信中であることを軽く示す（真っ白に消すのではなく既存内容を薄く見せた
   ままにすることで、体感速度・レイアウト崩れの防止を両立する）。 */
.tc-pjax-overlay{position:absolute;inset:0;background:rgba(255,255,255,0.5);z-index:10;cursor:progress;}
/* 【2026-09-04追記：「最近の記事」で閲覧中タイトルの赤色ハイライト】 */
#block-3 a.wp-block-latest-posts__post-title.nseb-recent-active{color:#d32f2f !important;font-weight:bold;}
</style>
<script>
(function () {
  function computePageList(current, last) {
    var pages = [];
    if (last <= 10) {
      for (var i = 1; i <= last; i++) { pages.push(i); }
      return pages;
    }
    if (current <= 6) {
      for (var i = 1; i <= 9; i++) { pages.push(i); }
      pages.push('...');
      pages.push(last);
    } else if (current >= last - 5) {
      pages.push(1);
      pages.push('...');
      for (var i = last - 8; i <= last; i++) { pages.push(i); }
    } else {
      pages.push(1);
      pages.push('...');
      for (var i = current - 3; i <= current + 3; i++) { pages.push(i); }
      pages.push('...');
      pages.push(last);
    }
    return pages;
  }

  // 【2026-09-02追記：ソート×ページネーション連動バグの修正】
  // WordPress core の paginate_links() が、現在ページが /page/N/ 形式の
  // パスベースになっている状態から他ページへのリンクを生成する際、本来不要な
  // "&paged=1" をクエリ文字列側に残留させてしまう（実機で確認済みのWordPress側の
  // 挙動）。この残留paged=1を含むURLへ遷移すると、WordPressはパス側のページ番号
  // よりクエリ文字列側のpaged=1を優先してしまい、結果的に1ページ目へ正規化
  // （リダイレクト）される＝「並び替えを選んで3ページ目以降へ進むと1ページ目に
  // 戻る」という報告された不具合の直接原因だった。対策として、URL APIで
  // クエリパラメータを正しく解析し、まず既存のpaged/pagedを完全に取り除いてから
  // 目的のページ番号を付け直す（正規表現の文字列置換に頼らないことで、
  // 残留パラメータの位置や個数に関わらず確実に正規化する）。
  function urlForPage(sampleHref, page) {
    var url;
    try {
      url = new URL(sampleHref, window.location.href);
    } catch (e) {
      return sampleHref;
    }
    url.searchParams.delete('paged');
    var path = url.pathname.replace(/\/page\/\d+\/?$/, '/');
    if (page > 1) {
      path = (path.charAt(path.length - 1) === '/' ? path : path + '/') + 'page/' + page + '/';
    }
    url.pathname = path;
    return url.pathname + url.search + url.hash;
  }

  function buildPagerEl(current, last, sampleHref) {
    var pages = computePageList(current, last);
    var wrap = document.createElement('div');
    wrap.className = 'tc-pager';

    if (current > 1) {
      var prev = document.createElement('a');
      prev.className = 'tc-pager-arrow';
      prev.href = urlForPage(sampleHref, current - 1);
      prev.textContent = '‹';
      wrap.appendChild(prev);
    }

    pages.forEach(function (p) {
      if (p === '...') {
        var dots = document.createElement('span');
        dots.className = 'tc-pager-dots';
        dots.textContent = '…';
        wrap.appendChild(dots);
        return;
      }
      if (p === current) {
        var cur = document.createElement('span');
        cur.className = 'tc-pager-num tc-pager-current';
        cur.textContent = String(p);
        wrap.appendChild(cur);
        return;
      }
      var a = document.createElement('a');
      a.className = 'tc-pager-num';
      a.href = urlForPage(sampleHref, p);
      a.textContent = String(p);
      wrap.appendChild(a);
    });

    if (current < last) {
      var next = document.createElement('a');
      next.className = 'tc-pager-arrow';
      next.href = urlForPage(sampleHref, current + 1);
      next.textContent = '›';
      wrap.appendChild(next);
    }
    return wrap;
  }

  function render(nav) {
    var numberEls = nav.querySelectorAll('.page-numbers:not(.dots):not(.prev):not(.next)');
    if (!numberEls.length) { return null; }

    var current = 1, last = 1, sampleHref = null;
    numberEls.forEach(function (el) {
      var n = parseInt((el.textContent || '').trim(), 10);
      if (!n) { return; }
      if (n > last) { last = n; }
      if (el.classList.contains('current')) { current = n; }
      if (el.tagName === 'A' && !sampleHref) { sampleHref = el.getAttribute('href'); }
    });
    if (last <= 1 || !sampleHref) { return null; }

    var wrap = buildPagerEl(current, last, sampleHref);
    nav.innerHTML = '';
    nav.appendChild(wrap);
    return wrap;
  }

  // 【2026-09-02追記：一覧上部の並び替えタブ】現在のパス（カテゴリー階層を
  // 含む）はそのまま維持し、?sort=のみを差し替える。ページ番号は並び替え
  // 変更時に1ページ目へ戻す（?page/paged系のパラメータ・パスセグメントを
  // 取り除く）のが自然なUXのため、そのように組み立てる。
  function currentSort() {
    try {
      var v = new URLSearchParams(window.location.search).get('sort');
      return (v === 'pv' || v === 'like') ? v : 'new';
    } catch (e) { return 'new'; }
  }
  function basePathWithoutPage() {
    var path = window.location.pathname.replace(/\/page\/\d+\/?$/, '/');
    var params = new URLSearchParams(window.location.search);
    params.delete('paged');
    params.delete('sort');
    var qs = params.toString();
    return path + (qs ? '?' + qs : '');
  }
  function buildSortTabs() {
    var base = basePathWithoutPage();
    var sep = base.indexOf('?') === -1 ? '?' : '&';
    var options = [
      { key: 'new', label: '新着順' },
      { key: 'pv', label: '閲覧数順' },
      { key: 'like', label: 'スキ数順' },
    ];
    var active = currentSort();
    var tabs = document.createElement('div');
    tabs.className = 'tc-sort-tabs';
    options.forEach(function (opt) {
      var a = document.createElement('a');
      a.className = 'tc-sort-tab' + (opt.key === active ? ' tc-sort-tab-active' : '');
      a.href = opt.key === 'new' ? base : (base + sep + 'sort=' + opt.key);
      a.textContent = opt.label;
      tabs.appendChild(a);
    });
    return tabs;
  }

  // 【2026-09-02追記：視点位置の固定】ページ送り・並び替え・カテゴリー選択の
  // 「結果としての遷移」でだけ、記事一覧の先頭（.tc-top-controls）が見える
  // 位置まで自動スクロールする。PJAX成功時はここで直接呼ぶ。PJAXが使えず
  // 通常のフルページ遷移にフォールバックした場合のため、クリック時に
  // sessionStorageへ目印を立てておき、次回ロード時にそれを見て判定する
  // フォールバック経路も維持する。
  var SCROLL_FLAG_KEY = 'nseb_scroll_to_content';
  function scrollToContentNow() {
    var target = document.querySelector('.tc-top-controls') || document.querySelector('#list, .list');
    if (!target) { return; }
    var y = target.getBoundingClientRect().top + window.scrollY - 20;
    try { window.scrollTo({ top: Math.max(0, y), behavior: 'smooth' }); }
    catch (e) { window.scrollTo(0, Math.max(0, y)); }
  }
  function maybeScrollToContentFromFlag() {
    var flagged;
    try { flagged = sessionStorage.getItem(SCROLL_FLAG_KEY) === '1'; } catch (e) { flagged = false; }
    if (!flagged) { return; }
    try { sessionStorage.removeItem(SCROLL_FLAG_KEY); } catch (e) {}
    scrollToContentNow();
  }

  // 【2026-09-02追記：一覧エリアの初期化（PJAXでの差し替え後にも再実行する）】
  // ネイティブの.paginationをツイキャス風ページャーに変換し、上部に
  // 並び替えタブ＋ページャーを新設する。PJAXでこの範囲（#main）を丸ごと
  // 差し替えた直後は毎回サーバー生成のそのままの状態（変換前）に戻るため、
  // 差し替えのたびに必ずこの関数を呼び直す必要がある。
  function enhanceListingArea() {
    var navs = document.querySelectorAll('nav.pagination, .pagination');
    var firstWrap = null;
    navs.forEach(function (nav) {
      var w = render(nav);
      if (w && !firstWrap) { firstWrap = w; }
    });

    var list = document.querySelector('#list, .list');
    if (list && list.parentElement) {
      var topBlock = document.createElement('div');
      topBlock.className = 'tc-top-controls';
      topBlock.appendChild(buildSortTabs());
      if (firstWrap) {
        topBlock.appendChild(firstWrap.cloneNode(true));
      }
      list.parentElement.insertBefore(topBlock, list);
    }
  }

  // ==================== 【2026-09-02追記：PJAXによる一覧の非同期部分更新】 ====================
  // サイドバー（#sidebar）は常にDOMに触れず、記事一覧を含む#mainの中身だけを
  // fetch()で取得したHTMLの#mainの中身に差し替える。サイドバーのスクロール
  // 位置・アコーディオンの開閉状態は、要素自体を一切再生成しないため自然に
  // そのまま維持される。取得・解析・置換のいずれかで失敗した場合は、
  // 通常のフルページ遷移へ安全にフォールバックする（機能停止を避けるため）。
  var MAIN_SELECTOR = '#main';
  var pjaxInFlight = false;

  function showLoadingOverlay(mainEl) {
    if (getComputedStyle(mainEl).position === 'static') {
      mainEl.style.position = 'relative';
    }
    var overlay = document.createElement('div');
    overlay.className = 'tc-pjax-overlay';
    mainEl.appendChild(overlay);
    return overlay;
  }

  // 【2026-09-06追記：仕様再徹底】前回導入した「サイドバー内クリックは構造を
  // 一切変更しない（fromSidebar抑制）」方式は、タグが所属する他カテゴリーの
  // 青色展開まで消してしまう副作用があり、ユーザーの本来の要望
  // （他カテゴリーも展開して青色で見せる）と矛盾していたため撤回した。
  // 代わりに、展開ロジックは常にフルで実行しつつ、展開の結果アクティブ
  // （赤）要素が視界から外れた場合にサイドバー自身のスクロール位置だけを
  // 補正して視界内へ戻す方式（keepActiveInView、各ウィジェット側で実装）に
  // 差し替えた。これによりページ送り（.tc-pager）等サイドバー外からの
  // 遷移でも、展開後に赤色アクティブ項目が必ず見える位置へ自動的に
  // 収まる。
  function pjaxNavigate(url, push) {
    var mainEl = document.querySelector(MAIN_SELECTOR);
    if (!mainEl || pjaxInFlight) {
      window.location.href = url;
      return;
    }
    pjaxInFlight = true;
    var overlay = showLoadingOverlay(mainEl);

    fetch(url, { credentials: 'same-origin' })
      .then(function (res) {
        if (!res.ok) { throw new Error('http ' + res.status); }
        return res.text();
      })
      .then(function (htmlText) {
        var doc = new DOMParser().parseFromString(htmlText, 'text/html');
        var newMain = doc.querySelector(MAIN_SELECTOR);
        if (!newMain) { throw new Error('main not found in response'); }

        mainEl.innerHTML = newMain.innerHTML;
        if (doc.title) { document.title = doc.title; }

        enhanceListingArea();
        // 【2026-09-07追記：カテゴリ×タグ複合絞り込み】記事カテゴリーウィジェット
        // 側はタグの所属カテゴリーを ?cat= クエリで判定するため、pathnameだけで
        // なくsearch（クエリ文字列）も渡す。年代別アーカイブ側はクエリ文字列を
        // 一切使わない（月リンクは常にパスのみ）ため、従来通りpathnameのみ渡す。
        if (typeof window.nsebUpdateSidebarActiveState === 'function') {
          try {
            var _navUrl = new URL(url, window.location.href);
            window.nsebUpdateSidebarActiveState(_navUrl.pathname + _navUrl.search);
          }
          catch (e) {}
        }
        // 【2026-09-03追記】年代別アーカイブウィジェットのアクティブ状態も、
        // 記事カテゴリーウィジェットと同じタイミングで更新する。
        if (typeof window.nsebUpdateArchiveActiveState === 'function') {
          try { window.nsebUpdateArchiveActiveState(new URL(url, window.location.href).pathname); }
          catch (e) {}
        }
        // 【2026-09-02追記】note-style-engagement.phpのカード価格/PV/スキ数
        // バッジは、サーバーHTMLに一切含まれずJSのみで生成されるため、
        // #mainを差し替えるたびに必ず再実行しないと新しいカードが
        // 空（メタ情報無し）のままになる（実機で確認済みの不具合）。
        if (typeof window.nsebRefreshAll === 'function') {
          try { window.nsebRefreshAll(); }
          catch (e) {}
        }
        highlightRecentPosts(new URL(url, window.location.href).pathname);

        if (push) { window.history.pushState({ nsebPjax: true }, '', url); }
        try { sessionStorage.removeItem(SCROLL_FLAG_KEY); } catch (e) {}
        scrollToContentNow();
      })
      .catch(function () {
        // フェッチ・解析・#main抽出のいずれかで失敗した場合は通常遷移で必ず結果を届ける。
        window.location.href = url;
      })
      .finally(function () {
        pjaxInFlight = false;
        if (overlay && overlay.parentElement) { overlay.parentElement.removeChild(overlay); }
      });
  }

  function isPlainLeftClick(e) {
    return e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey;
  }

  // 【2026-09-02追記：「次のページ」ボタンの取りこぼし修正】Cocoon純正の
  // 大きな「次のページ」ボタン（.pagination-next-link）は、ツイキャス風に
  // 変換する対象の.paginationとは別の独立した要素のため、このセレクタに
  // 含めていなかった分だけPJAXが効かずフルリロードしてしまっていた
  // （実機で報告・確認済み）。hrefは既にCocoon側で正しく生成されている
  // （sort等のクエリパラメータも維持された状態）ため、変換不要でそのまま
  // pjaxNavigateへ渡せる。documentへのイベント委譲のため、PJAXで#mainの
  // 中身が丸ごと差し替わり、新しい「次のページ」ボタンが挿入された後でも
  // 登録し直す必要なく引き続き捕捉できる。
  // 【2026-09-02追記】サイドバー上部の「🏠 ホーム」ボタン（#nseb-home-link）も
  // PJAX対象に追加。ホームへの遷移後、nsebUpdateSidebarActiveStateは
  // どのカテゴリーリンクとも一致しなくなるため、既存のactiveクラス除去ロジック
  // だけで自然にサイドバーの選択状態がクリアされる（追加のクリア処理は不要）。
  // 【2026-09-03追記】年代別アーカイブの月リンク（.nseb-archive-month-link）も
  // PJAX対象に追加。
  var PJAX_LINK_SELECTOR = '.tc-pager a, .tc-sort-tabs a, .cat-accordion-widget a[data-cat-slug], .pagination-next-link, #nseb-home-link, .nseb-archive-month-link';
  document.addEventListener('click', function (e) {
    var link = e.target.closest(PJAX_LINK_SELECTOR);
    if (!link) { return; }
    var href = link.getAttribute('href');
    if (!href || link.target === '_blank') { return; }
    // フォールバック（通常遷移）に回った場合のための保険。PJAXが成功すれば
    // pjaxNavigate内で即座に消すため、フルページ遷移でしか使われない。
    try { sessionStorage.setItem(SCROLL_FLAG_KEY, '1'); } catch (err) {}
    if (!isPlainLeftClick(e)) { return; } // 新規タブ等の意図はそのまま尊重する
    e.preventDefault();
    // 【2026-09-08追記：ホームボタンでのサイドバースクロール初期化】
    // #nseb-home-linkはPJAX遷移のため、通常のフルページ遷移で効く
    // isPlainHomeVisit()（persistSidebarScrollの中）のホーム判定を経由しない。
    // ここで明示的にscrollTopを0へ戻し、保存値も消してから遷移させることで、
    // 「ホームボタンを押したのに深いスクロール位置のまま」という不具合を防ぐ。
    if (link.id === 'nseb-home-link') {
      var _homeSidebar = document.querySelector('.sidebar');
      if (_homeSidebar) { _homeSidebar.scrollTop = 0; }
      try { sessionStorage.removeItem(SIDEBAR_SCROLL_KEY); } catch (err) {}
    }
    pjaxNavigate(link.href, true);
  }, true);

  window.addEventListener('popstate', function () {
    pjaxNavigate(window.location.href, false);
  });

  // 【2026-09-05追記：Universal State Saver】サイドバー内のどのリンク
  // （最近の記事・カテゴリー・タグ・マイ本棚など、PJAX対象かフルページ遷移かを
  // 問わない）をクリックしても、離脱前に必ずその瞬間のscrollTopを同期的に
  // 保存する。既存のpersistSidebarScroll()内のscroll イベント保存は150ms
  // デバウンスのため、「スクロール直後に間髪入れずクリック」された場合に
  // 保存が間に合わず、古い値が復元されてしまう競合状態が理論的に存在した
  // （開閉状態(setNodeOpen)は元々トグル時に同期保存済みで問題なし）。
  // クリック時点で即座に保存することでこの競合を完全に無くす。キャプチャ
  // フェーズで登録し、preventDefaultの有無や後続のリンク種別を問わず
  // 必ず実行されるようにする。
  document.addEventListener('click', function (e) {
    var insideSidebar = e.target.closest ? e.target.closest('.sidebar') : null;
    if (!insideSidebar) { return; }
    var sidebar = document.querySelector('.sidebar');
    if (!sidebar) { return; }
    try { sessionStorage.setItem(SIDEBAR_SCROLL_KEY, String(sidebar.scrollTop)); } catch (err) {}
  }, true);

  // 【2026-09-02追記：記事詳細往復時のサイドバースクロール位置復元】
  // 記事詳細ページへ実際に遷移（フルページ遷移）してブラウザバックで戻ると、
  // 離脱時にJSのメモリ状態・DOMは失われ、戻り先URLはブラウザによって
  // サーバーから丸ごと再読み込みされる（PJAXのpushState管理下のURLであっても、
  // 一度別ドキュメントへ遷移した後の「戻る」はブラウザが実文書を再取得する
  // ため回避できない）。そのため、この復元だけはPJAXでは対応できず、
  // sessionStorageでスクロール位置を都度保存し、次の読み込み時に復元する
  // 方式で対応する（PJAXのみのページ切り替えでは#sidebarのDOM自体に一切
  // 触れないため、この保存・復元処理を挟んでも実質無害＝常に同じ値を
  // 読み書きするだけで、通常時のスクロール位置を壊すことはない）。
  // 【2026-09-04追記：「最近の記事」で閲覧中タイトルの赤色ハイライト】
  // #block-3（Cocoon/Gutenbergの「最近の投稿」動的ブロック）はPHPで固定
  // マークアップを生成できないコアブロックのため、静的HTMLへのクラス埋め込みが
  // できない。現在のURL（window.location.pathname）と各投稿タイトルリンクの
  // href（絶対URL）を比較し、一致するものにだけ赤色クラスを付与する方式で
  // 対応する。フルページ遷移（記事詳細へ実際に遷移するケース）・PJAX双方の
  // 遷移後に呼び直せるよう、通常のsessionStorage系状態とは独立した純粋な
  // 「現在のURLとの一致判定」のみで完結させる（記事詳細ページ閲覧中も
  // 常に呼ばれるため、他のウィジェットのような永続化は不要）。
  var RECENT_POST_ACTIVE_CLASS = 'nseb-recent-active';
  function highlightRecentPosts(pathname) {
    var path = pathname || window.location.pathname;
    document.querySelectorAll('#block-3 a.wp-block-latest-posts__post-title').forEach(function (a) {
      var linkPath;
      try { linkPath = new URL(a.getAttribute('href'), window.location.href).pathname; }
      catch (e) { linkPath = a.getAttribute('href'); }
      a.classList.toggle(RECENT_POST_ACTIVE_CLASS, linkPath === path);
    });
  }

  var SIDEBAR_SCROLL_KEY = 'nseb_sidebar_scroll';
  // 【2026-09-08追記：トップページ訪問時のスクロール初期化】
  // パスが「/」だけ（意味のあるクエリパラメータが無い、純粋なトップページ
  // アクセス）の場合は、過去の探索中に保存された深いスクロール位置を
  // 一切復元せず、必ず最上部（月額読み放題プラン等）が見えるようにする。
  // filter_submitted=1（検索）・view=my-library（マイ本棚）・sort=（並び替え）
  // 等、意味のあるパラメータが付いている場合はこの判定から除外し、従来通り
  // 位置を維持する（このヘルパーはpersistSidebarScrollとPJAXのホームボタン
  // ハンドラの両方から使う）。_e2eはE2Eテストのキャッシュバイパス専用の
  // パラメータのため無視する。
  function isPlainHomeVisit() {
    if (window.location.pathname !== '/') { return false; }
    var params = new URLSearchParams(window.location.search);
    params.delete('_e2e');
    return Array.from(params.keys()).length === 0;
  }

  function persistSidebarScroll() {
    var sidebar = document.querySelector('.sidebar');
    if (!sidebar) { return; }

    function applySaved() {
      var saved;
      try { saved = sessionStorage.getItem(SIDEBAR_SCROLL_KEY); } catch (e) { saved = null; }
      if (saved === null) { return; }
      var n = parseInt(saved, 10);
      if (!isNaN(n)) { sidebar.scrollTop = n; }
    }

    function applyActiveGuard() {
      if (typeof window.nsebKeepActiveInView === 'function') {
        try { window.nsebKeepActiveInView(); } catch (e) {}
      }
    }

    if (isPlainHomeVisit()) {
      try { sessionStorage.removeItem(SIDEBAR_SCROLL_KEY); } catch (e) {}
      sidebar.scrollTop = 0;
      // window.load時にレイアウト変化で再びズレないよう、こちらも0を再適用する。
      window.addEventListener('load', function () { sidebar.scrollTop = 0; }, { once: true });
    } else {
      applySaved();
      // 【2026-09-09追記：記事詳細ページで赤色タグが枠外に隠れる不具合の
      // 修正】このスクリプト（footer）はサイドバーHTML内で先に実行される
      // カテゴリー/アーカイブウィジェット側の<script>より必ず後に実行される。
      // そのウィジェット側は自分の初期化時にkeepActiveInView()で「アクティブ
      // （赤）要素が視界内に収まる」よう既にscrollTopを計算・設定済みだが、
      // 直後にこのapplySaved()が「保存済みの生のスクロールpx値」で無条件に
      // 上書きしてしまい、赤色タグが再び視界外へ押し出される競合を実機で
      // 確認した（例：ワクチン・免疫学検証配下の大谷翔平タグを選択→記事詳細
      // へ遷移すると、正しく計算されたscrollTopが直後に0へ戻され、赤色タグが
      // サイドバー下部7000px付近に隠れてしまっていた）。
      // 対策として、生のスクロール値を復元した「後に」ウィジェット側が公開する
      // nsebKeepActiveInView()を呼び直し、最終的な決定権を必ず「アクティブ
      // 要素が視界内に収まっているか」に持たせる（同関数は既に視界内であれば
      // 何もしないため、通常の探索中の位置維持を壊すことはない）。
      applyActiveGuard();

      // 【実機で確認した追加対策】Webフォントの読み込み完了等、DOMContentLoaded
      // より後に発生するレイアウト変化でサイドバー内の高さが変わると、
      // ブラウザ標準のスクロールアンカリング機能がscrollTopを勝手に補正して
      // しまい、せっかく復元した位置からズレることを実機で確認した（260px→679px
      // にずれる事象を再現）。.sidebarにoverflow-anchor:noneを付けて標準機能を
      // 無効化した上で、保険としてwindow.load（全リソース読み込み完了）時にも
      // 一度だけ再適用する。
      window.addEventListener('load', function () {
        applySaved();
        applyActiveGuard();
      }, { once: true });
    }

    var saveTimer = null;
    sidebar.addEventListener('scroll', function () {
      if (saveTimer) { clearTimeout(saveTimer); }
      saveTimer = setTimeout(function () {
        try { sessionStorage.setItem(SIDEBAR_SCROLL_KEY, String(sidebar.scrollTop)); } catch (e) {}
      }, 150);
    });
  }

  function init() {
    enhanceListingArea();
    maybeScrollToContentFromFlag();
    persistSidebarScroll();
    highlightRecentPosts();
  }
  if (document.readyState !== 'loading') { init(); }
  else { document.addEventListener('DOMContentLoaded', init); }
})();
</script>
        <?php
    }
}

new Custom_Search_Category_Filter();
