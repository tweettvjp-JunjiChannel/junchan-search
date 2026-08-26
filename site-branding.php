<?php
/**
 * Plugin Name: Junchan World Site Branding
 * Description: ヘッダーロゴ（サイトタイトル）に丸型プロフィール写真とタグラインを統合し、グローバルナビの「準備中」項目にツールチップ・クリック無効化を付与する。
 * Version: 1.1.0
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
#header-in h1.logo.logo-header {
  grid-row: 1;
  grid-column: 2;
  margin: 0;
  text-align: left;
}
#header-in h1.logo.logo-header .site-name-text {
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
  #header-in h1.logo.logo-header .site-name-text {
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

/* サイドバーウィジェットのアコーディオン開閉UI */
.widget h2.wp-block-heading.jw-widget-toggle {
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: space-between;
  user-select: none;
}
.jw-toggle-icon {
  font-size: 0.7em;
  color: #999;
  margin-left: 8px;
  flex-shrink: 0;
}
.jw-archive-groups {
  font-size: 0.92em;
}
.jw-archive-group-header {
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 4px;
  border-bottom: 1px solid #eee;
  font-weight: bold;
  user-select: none;
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

  function makeToggleIcon() {
    var icon = document.createElement('span');
    icon.className = 'jw-toggle-icon';
    icon.textContent = '▼';
    return icon;
  }

  // 「最近の記事」「最近のコメント」（5件ずつ増える）・「カテゴリ」（全件一括）
  // 共通のサイドバーウィジェット開閉ロジック。
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
      heading.appendChild(makeToggleIcon());
      var icon = heading.querySelector('.jw-toggle-icon');

      var items = (content.tagName === 'UL') ? Array.prototype.slice.call(content.children) : null;
      var total = items ? items.length : 0;
      var shown = 0;

      (function (content, items, total, icon, incremental, step) {
        function render() {
          if (incremental && items && total > 0) {
            for (var i = 0; i < items.length; i++) {
              items[i].style.display = (i < shown) ? '' : 'none';
            }
            content.style.display = shown > 0 ? '' : 'none';
            icon.textContent = (shown >= total) ? '▲' : '▼';
          } else {
            content.style.display = shown > 0 ? '' : 'none';
            icon.textContent = shown > 0 ? '▲' : '▼';
          }
        }
        function onToggle() {
          if (incremental && items && total > 0) {
            shown = (shown >= total) ? 0 : Math.min(shown + step, total);
          } else {
            shown = shown > 0 ? 0 : 1;
          }
          render();
        }
        heading.addEventListener('click', onToggle);
        content.style.display = 'none';
        render();
      })(content, items, total, icon, incremental, step);
    }
  }

  // 「アーカイブ」：既存の月別 <li> を5年区切りの2段アコーディオンに再構築する。
  function setupArchiveAccordion() {
    var headings = document.querySelectorAll('.widget h2.wp-block-heading');
    for (var h = 0; h < headings.length; h++) {
      var heading = headings[h];
      if ((heading.textContent || '').trim() !== 'アーカイブ') { continue; }
      if (heading.dataset.jwArchiveBound) { continue; }
      var content = heading.nextElementSibling;
      if (!content || content.tagName !== 'UL') { continue; }
      heading.dataset.jwArchiveBound = '1';

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

      function toNum(mo) { return mo.year * 12 + mo.month; }
      function fromNum(n) {
        var y = Math.floor((n - 1) / 12);
        var mm = ((n - 1) % 12) + 1;
        return { year: y, month: mm };
      }

      var newest = months[0];
      var oldest = months[months.length - 1];
      var newestNum = toNum(newest);

      var boundaries = [{ year: oldest.year, month: oldest.month }];
      var curNum = toNum(oldest);
      while (curNum + 60 <= newestNum) {
        curNum += 60;
        boundaries.push(fromNum(curNum));
      }
      var lastB = boundaries[boundaries.length - 1];
      if (!(lastB.year === newest.year && lastB.month === newest.month)) {
        boundaries.push({ year: newest.year, month: newest.month });
      }

      var groups = [];
      for (var b = 0; b < boundaries.length; b++) {
        var startNum = toNum(boundaries[b]);
        var endNum = (b + 1 < boundaries.length) ? (toNum(boundaries[b + 1]) - 1) : newestNum;
        var groupMonths = months.filter(function (mo) {
          var n = toNum(mo);
          return n >= startNum && n <= endNum;
        });
        if (groupMonths.length) {
          groups.push({ label: boundaries[b].year + '年' + boundaries[b].month + '月', months: groupMonths });
        }
      }
      groups.reverse();

      var wrapper = document.createElement('div');
      wrapper.className = 'jw-archive-groups';
      groups.forEach(function (g) {
        var groupHeader = document.createElement('div');
        groupHeader.className = 'jw-archive-group-header';
        var labelSpan = document.createElement('span');
        labelSpan.textContent = g.label;
        groupHeader.appendChild(labelSpan);
        groupHeader.appendChild(makeToggleIcon());
        var groupIcon = groupHeader.querySelector('.jw-toggle-icon');

        var groupList = document.createElement('ul');
        groupList.className = 'jw-archive-group-list';
        groupList.style.display = 'none';
        g.months.forEach(function (mo) {
          var li = document.createElement('li');
          var a = document.createElement('a');
          a.href = mo.href;
          a.textContent = mo.label;
          li.appendChild(a);
          groupList.appendChild(li);
        });

        groupHeader.addEventListener('click', function () {
          var isOpen = groupList.style.display !== 'none';
          groupList.style.display = isOpen ? 'none' : '';
          groupIcon.textContent = isOpen ? '▼' : '▲';
        });

        wrapper.appendChild(groupHeader);
        wrapper.appendChild(groupList);
      });

      content.parentNode.insertBefore(wrapper, content);
      content.style.display = 'none';
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
