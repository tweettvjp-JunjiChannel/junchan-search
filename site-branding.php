<?php
/**
 * Plugin Name: Junchan World Site Branding
 * Description: ヘッダーロゴ（サイトタイトル）に丸型プロフィール写真とタグラインを統合し、グローバルナビの「準備中」項目にツールチップ・クリック無効化を付与する。
 * Version: 1.0.0
 * Author: junchan-world
 */

if (!defined('ABSPATH')) {
    exit;
}

class Junchan_Site_Branding {

    const PHOTO_URL = 'https://junchan-world.com/wp-content/uploads/2026/08/junchan-header-photo.png';

    public function __construct() {
        add_action('wp_head', array($this, 'print_css'));
        add_action('wp_footer', array($this, 'print_js'));
    }

    public function print_css() {
        $photo = esc_url(self::PHOTO_URL);
        ?>
<style id="jw-site-branding-css">
#header-in {
  display: grid;
  grid-template-columns: auto 1fr;
  grid-template-rows: auto auto;
  align-items: center;
  column-gap: 14px;
  row-gap: 2px;
  text-align: left;
}
.jw-header-photo {
  grid-row: 1 / 3;
  grid-column: 1;
  width: 56px;
  height: 56px;
  border-radius: 50%;
  object-fit: cover;
  border: 3px solid #d4af37;
  box-shadow: 0 1px 4px rgba(0,0,0,0.25);
  background: #fff;
}
#header-in h1.logo.logo-header {
  grid-row: 1;
  grid-column: 2;
  margin: 0;
  text-align: left;
}
#header-in .tagline {
  grid-row: 2;
  grid-column: 2;
  font-size: 0.72em;
  color: #777;
  line-height: 1.4;
  text-align: left;
}
@media screen and (max-width: 600px) {
  .jw-header-photo {
    width: 40px;
    height: 40px;
    border-width: 2px;
  }
  #header-in {
    column-gap: 8px;
  }
  #header-in .tagline {
    font-size: 0.62em;
  }
}

/* 「準備中」ナビ項目：クリック無効化・見た目を弱める */
.navi .menu-item.jw-coming-soon > a {
  cursor: default;
  opacity: 0.6;
}
.navi .menu-item.jw-coming-soon > a:hover {
  opacity: 0.6;
}
</style>
        <?php
    }

    public function print_js() {
        $photo = esc_url(self::PHOTO_URL);
        ?>
<script id="jw-site-branding-js">
(function () {
  function ready(fn) {
    if (document.readyState !== 'loading') { fn(); }
    else { document.addEventListener('DOMContentLoaded', fn); }
  }

  function insertHeaderPhoto() {
    var headerIn = document.getElementById('header-in');
    if (!headerIn || headerIn.querySelector('.jw-header-photo')) { return; }
    var img = document.createElement('img');
    img.className = 'jw-header-photo';
    img.src = '<?php echo $photo; ?>';
    img.alt = '順ちゃんワールド';
    headerIn.insertBefore(img, headerIn.firstChild);
  }

  function markComingSoonLinks() {
    var links = document.querySelectorAll('.navi .menu-item > a');
    for (var i = 0; i < links.length; i++) {
      var a = links[i];
      var label = (a.textContent || '').trim();
      if (label.indexOf('準備中') !== -1) {
        var li = a.closest('.menu-item');
        if (li) { li.classList.add('jw-coming-soon'); }
        if (!a.getAttribute('title')) {
          a.setAttribute('title', '準備中：順次公開予定です');
        }
        if (!a.dataset.jwBound) {
          a.dataset.jwBound = '1';
          a.addEventListener('click', function (e) {
            e.preventDefault();
          });
        }
      }
    }
  }

  function refreshAll() {
    insertHeaderPhoto();
    markComingSoonLinks();
  }

  ready(refreshAll);
  window.addEventListener('pageshow', function (event) {
    if (event.persisted) { refreshAll(); }
  });
})();
</script>
        <?php
    }
}

new Junchan_Site_Branding();
