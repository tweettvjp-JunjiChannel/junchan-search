<?php
/**
 * Plugin Name: Junchan World Site Branding
 * Description: ヘッダーロゴ（サイトタイトル）に丸型プロフィール写真とタグラインを統合し、グローバルナビの「準備中」項目にツールチップ・クリック無効化を付与する。
 * Version: 1.3.0
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
  column-gap: 20px;
  row-gap: 4px;
  text-align: left;
  padding: 10px 0;
}
.jw-header-photo {
  grid-row: 1 / 3;
  grid-column: 1;
  width: 92px;
  height: 92px;
  border-radius: 50%;
  object-fit: cover;
  border: 4px solid #d4af37;
  box-shadow: 0 2px 8px rgba(0,0,0,0.3);
  background: #fff;
}
#header-in .logo.logo-header {
  grid-row: 1;
  grid-column: 2;
  margin: 0;
  text-align: left;
}
#header-in .logo.logo-header .site-name-text {
  font-size: 2em !important;
  font-weight: 700 !important;
  letter-spacing: 0.06em;
  text-shadow: 1px 1px 2px rgba(0,0,0,0.15);
  color: #333;
}
#header-in .tagline {
  grid-row: 2;
  grid-column: 2;
  font-size: 15px !important;
  color: #555;
  font-weight: 500;
  line-height: 1.4;
  text-align: left;
}
@media screen and (max-width: 600px) {
  .jw-header-photo {
    width: 56px;
    height: 56px;
    border-width: 3px;
  }
  #header-in {
    column-gap: 10px;
  }
  #header-in .logo.logo-header .site-name-text {
    font-size: 1.3em !important;
    letter-spacing: 0.03em;
  }
  #header-in .tagline {
    font-size: 12px !important;
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

/* 記事カード：抜粋文が長い場合に下部の日付・価格メタ情報と重なる不具合の修正。
   Cocoon純正の-webkit-line-clamp指定はdisplay:-webkit-boxが無いと機能しないため、
   ここで明示的に有効化して抜粋文の高さを確実にクランプする。 */
.entry-card-snippet {
  display: -webkit-box !important;
  -webkit-box-orient: vertical !important;
  overflow: hidden !important;
}
.entry-card-content {
  padding-bottom: 2.6em !important;
}

/* サイドバーウィジェットのアコーディオン開閉UI（↓・↑・×） */
.widget h2.wp-block-heading.jw-widget-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.jw-toggle-icons {
  display: inline-flex;
  gap: 6px;
  margin-left: 8px;
  flex-shrink: 0;
}
.jw-toggle-icon {
  cursor: pointer;
  user-select: none;
  font-size: 0.8em;
  line-height: 1;
  color: #666;
  background: #eee;
  border-radius: 4px;
  padding: 3px 7px;
}
.jw-toggle-icon:hover {
  background: #ddd;
}
.jw-archive-groups {
  font-size: 0.92em;
}
.jw-archive-group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 4px;
  border-bottom: 1px solid #eee;
  font-weight: bold;
}
.jw-archive-group-header:first-child {
  border-top: 1px solid #eee;
}
.jw-archive-group-list {
  list-style: none;
  margin: 0 0 0 0.5em;
  padding: 4px 0 4px 0.5em;
  border-left: 2px solid #eee;
}
.jw-archive-group-list li {
  padding: 3px 0;
  font-size: 0.94em;
}
.jw-archive-group-list a.jw-archive-current {
  font-weight: bold;
  color: #c0392b;
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

  function makeIconBtn(symbol, cls) {
    var el = document.createElement('span');
    el.className = 'jw-toggle-icon ' + cls;
    el.textContent = symbol;
    return el;
  }

  // 単純開閉（↓で開く／×で全閉じる）。「カテゴリ」「アーカイブ」の
  // 各階層（ウィジェット全体・年区切りの親ブロック）で共用する。
  function attachSimpleToggle(heading, content, opts) {
    opts = opts || {};
    var iconWrap = document.createElement('span');
    iconWrap.className = 'jw-toggle-icons';
    var downBtn = makeIconBtn('↓', 'jw-icon-down');
    var closeBtn = makeIconBtn('×', 'jw-icon-close');
    iconWrap.appendChild(downBtn);
    iconWrap.appendChild(closeBtn);
    heading.appendChild(iconWrap);

    var open = !!opts.startOpen;
    function render() {
      content.style.display = open ? '' : 'none';
      downBtn.style.display = open ? 'none' : '';
      closeBtn.style.display = open ? '' : 'none';
    }
    function setOpen(v) { open = v; render(); }
    downBtn.addEventListener('click', function (e) { e.stopPropagation(); setOpen(true); });
    closeBtn.addEventListener('click', function (e) { e.stopPropagation(); setOpen(false); });
    render();
    return { setOpen: setOpen, isOpen: function () { return open; } };
  }

  // 段階開閉（↓で5件ずつ増える／↑で5件減らす／×で全閉じる）。
  // 「最近の記事」「最近のコメント」用。
  function attachSteppedToggle(heading, content, step) {
    var iconWrap = document.createElement('span');
    iconWrap.className = 'jw-toggle-icons';
    var downBtn = makeIconBtn('↓', 'jw-icon-down');
    var upBtn = makeIconBtn('↑', 'jw-icon-up');
    var closeBtn = makeIconBtn('×', 'jw-icon-close');
    iconWrap.appendChild(downBtn);
    iconWrap.appendChild(upBtn);
    iconWrap.appendChild(closeBtn);
    heading.appendChild(iconWrap);

    var items = (content.tagName === 'UL') ? Array.prototype.slice.call(content.children) : null;
    var total = items ? items.length : 0;
    var shown = 0;

    function render() {
      if (items && total > 0) {
        for (var i = 0; i < items.length; i++) {
          items[i].style.display = (i < shown) ? '' : 'none';
        }
      }
      content.style.display = shown > 0 ? '' : 'none';
      downBtn.style.display = (shown < total) ? '' : 'none';
      upBtn.style.display = (shown > 0) ? '' : 'none';
      closeBtn.style.display = (shown > 0) ? '' : 'none';
    }
    downBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      shown = Math.min(shown + step, total);
      render();
    });
    upBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      shown = Math.max(shown - step, 0);
      render();
    });
    closeBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      shown = 0;
      render();
    });
    render();
  }

  // 「最近の記事」「最近のコメント」（段階開閉）・「カテゴリ」（単純開閉）
  // 共通のサイドバーウィジェット検出ロジック。
  function setupAccordionWidgets(headingText, opts) {
    opts = opts || {};
    var incremental = !!opts.incremental;
    var step = opts.step || 5;

    var headings = document.querySelectorAll('.widget h2.wp-block-heading');
    for (var h = 0; h < headings.length; h++) {
      var heading = headings[h];
      if ((heading.textContent || '').trim() !== headingText) { continue; }
      if (heading.dataset.jwAccordionBound) { continue; }
      var content = heading.nextElementSibling;
      if (!content) { continue; }
      heading.dataset.jwAccordionBound = '1';
      heading.classList.add('jw-widget-toggle');
      content.style.display = 'none';

      if (incremental) {
        attachSteppedToggle(heading, content, step);
      } else {
        attachSimpleToggle(heading, content, {});
      }
    }
  }

  // 「アーカイブ」：既存の月別 <li> を2年区切りの2段アコーディオンに再構築する。
  // 現在閲覧中の月別アーカイブページに該当する場合は、該当する年区分と
  // ウィジェット全体を自動的に開いた状態で表示する。
  function setupArchiveAccordion() {
    var headings = document.querySelectorAll('.widget h2.wp-block-heading');
    for (var h = 0; h < headings.length; h++) {
      var heading = headings[h];
      if ((heading.textContent || '').trim() !== 'アーカイブ') { continue; }
      if (heading.dataset.jwArchiveBound) { continue; }
      var content = heading.nextElementSibling;
      if (!content || content.tagName !== 'UL') { continue; }
      heading.dataset.jwArchiveBound = '1';
      heading.classList.add('jw-widget-toggle');

      var links = Array.prototype.slice.call(content.querySelectorAll(':scope > li > a'));
      var months = [];
      for (var i = 0; i < links.length; i++) {
        var text = (links[i].textContent || '').trim();
        var m = text.match(/(\d{4})年(\d{1,2})月/);
        if (!m) { continue; }
        months.push({
          year: parseInt(m[1], 10),
          month: parseInt(m[2], 10),
          href: links[i].getAttribute('href'),
          label: text
        });
      }
      if (!months.length) { continue; }

      var curMatch = window.location.pathname.match(/\/(\d{4})\/(\d{2})\//);
      var curYear = curMatch ? parseInt(curMatch[1], 10) : null;
      var curMonth = curMatch ? parseInt(curMatch[2], 10) : null;

      var newestYear = months[0].year;
      var oldestYear = months[months.length - 1].year;

      var groups = [];
      for (var y = newestYear; y >= oldestYear; y -= 2) {
        var loY = y - 1;
        var groupMonths = months.filter(function (mo) {
          return mo.year === y || mo.year === loY;
        });
        if (groupMonths.length) {
          groups.push({ label: y + '年〜' + loY + '年', hiY: y, loY: loY, months: groupMonths });
        }
      }

      var outerWrap = document.createElement('div');
      outerWrap.className = 'jw-archive-groups';
      var anyGroupOpen = false;

      groups.forEach(function (g) {
        var groupHeader = document.createElement('div');
        groupHeader.className = 'jw-archive-group-header';
        var labelSpan = document.createElement('span');
        labelSpan.textContent = g.label;
        groupHeader.appendChild(labelSpan);

        var groupList = document.createElement('ul');
        groupList.className = 'jw-archive-group-list';
        g.months.forEach(function (mo) {
          var li = document.createElement('li');
          var a = document.createElement('a');
          a.href = mo.href;
          a.textContent = mo.label;
          if (curYear !== null && mo.year === curYear && mo.month === curMonth) {
            a.classList.add('jw-archive-current');
          }
          li.appendChild(a);
          groupList.appendChild(li);
        });

        var isCurrentGroup = curYear !== null && curYear <= g.hiY && curYear >= g.loY;
        if (isCurrentGroup) { anyGroupOpen = true; }
        attachSimpleToggle(groupHeader, groupList, { startOpen: isCurrentGroup });

        outerWrap.appendChild(groupHeader);
        outerWrap.appendChild(groupList);
      });

      content.parentNode.insertBefore(outerWrap, content);
      content.style.display = 'none';
      attachSimpleToggle(heading, outerWrap, { startOpen: anyGroupOpen });
    }
  }

  function refreshAll() {
    insertHeaderPhoto();
    markComingSoonLinks();
    setupAccordionWidgets('最近の記事', { incremental: true, step: 5 });
    setupAccordionWidgets('最近のコメント', { incremental: true, step: 5 });
    setupAccordionWidgets('カテゴリ', { incremental: false });
    setupArchiveAccordion();
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
