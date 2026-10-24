<?php
/* ============================================================
 * wp-config.php に追記する推奨セキュリティ設定（順ちゃんワールド）
 * 作成：2026-10-24 Claude Code（セキュリティ監査）
 *
 * 貼り付け位置：wp-config.php 内の
 *   「/* 編集が必要なのはここまでです ！ WordPress でのパブリッシングをお楽しみください。 *\/」
 *   （英語版なら「That's all, stop editing!」）の行より「上」。
 * 既に同じ define がある場合は二重定義になるので、既存の行を書き換えること。
 * 先頭の「<?php」はこのファイル単体の表示用。wp-config.php には貼らない。
 * ============================================================ */

/* --- 1. エラー表示を画面に出さない（PHPのパス・内部情報の漏えい防止） --- */
define( 'WP_DEBUG', false );
define( 'WP_DEBUG_DISPLAY', false );
define( 'WP_DEBUG_LOG', false );   // 調査時のみ true（wp-content/debug.log に出る。終わったら必ず false に戻す）
@ini_set( 'display_errors', '0' );

/* --- 2. 管理画面からのプラグイン/テーマ新規インストール・更新を禁止するか --- */
/* ※注意：DISALLOW_FILE_MODS を true にすると「プラグインの更新」「WordPress本体の自動更新」も止まる。
 *   順ちゃんワールドでは使わない（自動更新を止めるデメリットの方が大きい）。 */
// define( 'DISALLOW_FILE_MODS', true );

/* --- 3. 管理画面の「テーマファイルエディター」「プラグインファイルエディター」を無効化 --- */
/* ★重要★ 現在の運用（deploy_plugin.py などによるプラグイン反映、Cocoonソースの調査）は
 *   wp-admin の plugin-editor.php / theme-editor.php を使っている。
 *   true にするとこれらの自動デプロイが動かなくなるため、下の行はコメントアウトのまま置いておき、
 *   デプロイ手段を FTP/SFTP 等へ切り替えた後に有効化すること。 */
// define( 'DISALLOW_FILE_EDIT', true );

/* --- 4. 管理画面・ログインを常にHTTPSで --- */
define( 'FORCE_SSL_ADMIN', true );

/* --- 5. 本体のマイナー（セキュリティ）更新は自動適用（既定動作を明示） --- */
define( 'WP_AUTO_UPDATE_CORE', 'minor' );

/* --- 6. 投稿リビジョンの上限（DB肥大化対策・任意） --- */
define( 'WP_POST_REVISIONS', 10 );

/* --- 7. 認証キー（AUTH_KEY 等）について --- */
/* 漏えいが疑われる場合のみ https://api.wordpress.org/secret-key/1.1/salt/ で新しい値を生成し、
 * 既存の8行（AUTH_KEY〜NONCE_SALT）と置き換える。置き換えると全員ログアウトされる（アプリケーション
 * パスワードは影響を受けない）。 */
