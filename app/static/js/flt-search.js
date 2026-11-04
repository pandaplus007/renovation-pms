/* ============================================================================
 *  筛选下拉（漏斗多选）的搜索框
 * ============================================================================
 *
 *  这类下拉不是 <select>，而是 _multiselect.html 宏渲染的
 *  「按钮 + dropdown-menu + 一堆复选框 + 全选」。所以上面那个
 *  searchable-select 管不到它，这里单独做一份。
 *
 *  为什么值得单独做
 *  --------------
 *  供应商（9 家）、项目（13 个）、分类（10 个）这些维度一多，
 *  在 280px 高的菜单里靠滚轮找公司名是件很痛的事 ——
 *  而这恰恰是这套系统最常用的入口。
 *
 *  行为上的三个决定
 *  ----------------
 * 1. **只过滤，不自动勾选**。用户搜「深圳」看到 5 家公司，仍然要自己勾。
 *    搜完就全选是最容易出错的（按了回车结果命中 30 条全勾上）。
 * 2. **「全选」复选框不参与过滤**。它是控制项不是数据项，
 *    跟着一起被滤掉的话会出现「我明明没勾它却显示未勾全」的错觉。
 * 3. **清空搜索词后要恢复**。不能只做单向过滤，否则清空后列表还是空的。
 *
 *  匹配范围：label 文本 + option 的 value。
 *    value 常常是 id，匹配它没有意义但也不有害；留着是为了
 *    「分类」这种 value 就是中文本身的情况。
 * ========================================================================== */
(function () {
  'use strict';

  function norm(s) {
    return String(s == null ? '' : s).replace(/　/g, ' ').replace(/\s+/g, ' ').trim();
  }

  /**
   * 给一个 dropdown-menu 装上搜索框。
   * @param {HTMLElement} menu  .dropdown-menu
   */
  function attach(menu) {
    if (!menu || menu.dataset.ssSearch) return;
    var rows = Array.prototype.slice.call(
      menu.querySelectorAll('.form-check:not(.ms-all)')
    );
    // 2 项以内给搜索框是噪音
    if (rows.length < 4) return;

    menu.dataset.ssSearch = '1';
    menu.classList.add('ms-searchable');

    var box = document.createElement('div');
    box.className = 'ms-search-box';
    var input = document.createElement('input');
    input.type = 'text';
    input.className = 'form-control form-control-sm ms-search';
    input.placeholder = '输入以筛选…';
    input.autocomplete = 'off';
    input.setAttribute('aria-label', '筛选' + (rows.length) + '个选项');
    var ic = document.createElement('i');
    ic.className = 'bi bi-search';
    box.appendChild(ic);
    box.appendChild(input);

    // 插在「全选」那一行之后、数据行之前。
    //   位置很讲究：全选要在搜索框**上面**，因为它是「作用于当前可见列表」
    //   的动作；放下面的话用户搜完会习惯性往上看全选状态。
    var allRow = menu.querySelector('.form-check.ms-all');
    if (allRow && allRow.parentNode === menu) {
      menu.insertBefore(box, allRow.nextSibling);
    } else {
      menu.insertBefore(box, menu.firstChild);
    }

    var empty = document.createElement('div');
    empty.className = 'ms-search-empty small text-muted';
    empty.hidden = true;
    menu.appendChild(empty);

    function apply() {
      var q = norm(input.value).toLowerCase();
      var shown = 0;
      rows.forEach(function (row) {
        var cb = row.querySelector('input[type=checkbox]');
        var text = norm(row.textContent).toLowerCase();
        var val = cb ? norm(cb.value).toLowerCase() : '';
        var ok = !q || text.indexOf(q) >= 0 || val.indexOf(q) >= 0;
        row.hidden = !ok;
        if (ok) shown++;
      });
      empty.hidden = shown !== 0;
      if (shown === 0) empty.textContent = '没有匹配「' + norm(input.value) + '」的选项';
    }

    input.addEventListener('input', apply);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        // Esc 先清搜索词，再按一次才关菜单 —— 与「第二次才放弃」的习惯一致
        if (input.value) { e.stopPropagation(); input.value = ''; apply(); }
      }
    });
    // 下拉一关就把搜索词清掉，否则下次打开是上次的残留结果，
    // 看起来像「筛选没生效」。
    menu.addEventListener('hidden.bs.dropdown', function () { input.value = ''; apply(); });

    apply();
  }

  function scan(root) {
    var scope = root || document;
    Array.prototype.slice.call(scope.querySelectorAll('.dropdown-menu'))
      .forEach(attach);
  }

  window.FltSearch = { scan: scan };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { scan(); });
  } else {
    scan();
  }
})();
