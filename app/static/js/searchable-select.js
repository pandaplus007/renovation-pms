/* ============================================================================
 *  可搜索下拉（searchable select）—— 全站增强原生 <select>
 * ============================================================================
 *
 *  为什么自己写而不是引 TomSelect / Select2
 * ---------------------------------------
 * 1. **原生 <select> 留在 DOM 里当唯一数据源**。所有行为（表单提交、GET 筛选
 *    的 onchange、打印、导出）都还是原生那条路径，JS 只在外面加一层壳。
 *    后果：
 *      · JS 挂了 / 被 CDN 挡了 / 用户禁用 JS —— 页面照常能用，还是原生下拉
 *      · 后端一行不用改（收的还是同名参数）
 *      · 现有测试（对比度、浅色面、状态契约）照常能在 DOM 里找到那些 select
 *    引库的话这些全要重做：库的 DOM 里根本没有 <select>，表单得自己同步。
 * 2. 样式全部走本项目已有的 CSS 变量，主题切换自动跟随，不存在「库里那套
 *    默认白底在深色下变成一块补丁」的问题（这个坑 flatpickr 已经踩过一次，
 *    见 base.html 里切主题要重建日历的那段注释）。
 *
 *  什么时候才接管
 *  --------------
 *  **按选项数量**，不是逐个页面标。判据是 `options.length >= MIN_OPTIONS`：
 *    0~4 项（是否、付款方式、状态）  → 保持原生。给它加个输入框是纯噪音，
 *                                    用户还得先点开再打字才能选第 2 项。
 *    ≥8 项（材料 191、客户、项目、合同 49、分类 9）→ 接管。这些才是真痛点。
 *  例外可以显式关掉：给 select 加 `data-no-search`。
 *
 *  ⚠ 绝不能碰的三类（漏了会直接把日期选择器搞坏）：
 *    1. flatpickr 自己生成的月份下拉 —— `<select class="flatpickr-monthDropdown-months">`
 *       有 12 个选项，**会被 8 这个阈值命中**。它 `tabindex="-1"`、
 *       `aria-label="Month"`，且父级是 `.flatpickr-current-month`。
 *       接管它 = flatpickr 的月份选择直接失效（因为原 select 被移出日历容器）。
 *    2. `multiple` 的多选框（changes/form.html 有一个）—— 语义完全不同，
 *       换成一个单选列表是错的。
 *    3. `size > 1` 的列表框。
 *
 *  键盘（原生 select 里有，这里必须补回来，否则是**无障碍倒退**）
 *  ----------------------------------------------------------
 *    打开面板时焦点在搜索框：
 *      ↑ / ↓      移动高亮（不移动焦点，用 aria-activedescendant）
 *      Enter      选中高亮项并关闭
 *      Esc        关闭并把焦点还给触发按钮
 *      Home/End   跳到首/末
 *    关闭时焦点在触发按钮：
 *      Enter / Space / ↓   打开
 *      ↑ / ↓ / Home / End  不打开，直接换值（同原生 select 的行为）
 *      字母 / 汉字         跳到第一个以该字符开头的项（同原生 select）
 *
 *  定位为什么用 position:fixed
 *  ------------------------
 *  筛选栏经常落在 `.table-responsive`（overflow:auto）里，普通
 *  `position:absolute` 的面板会被容器裁掉 —— 表现为「下拉能打开但列表只有
 *  一半」。fixed 绕开所有 overflow 裁剪，代价是要自己算位置并在滚动时重算。
 *  唯一的失效场景是祖先有 transform/filter/perspective（会建立包含块），
 *  本项目没有。
 * ========================================================================== */
(function () {
  'use strict';

  /** 选项数达到这个值才接管。调大 = 更多下拉保持原生。 */
  var MIN_OPTIONS = 8;

  /** 打开时列表最多显示多高（px）。 */
  var MAX_LIST_H = 268;

  var openInstance = null;   // 同时只允许一个面板打开

  // --------------------------------------------------------------------------
  //  是否该接管
  // --------------------------------------------------------------------------
  function shouldSkip(sel) {
    if (!sel.tagName || sel.tagName.toLowerCase() !== 'select') return true;
    if (sel.multiple) return true;                       // 多选，语义不同
    if (sel.size > 1) return true;                       // 列表框
    if (sel.hasAttribute('data-no-search')) return true; // 显式关闭
    if (sel.dataset.ssReady) return true;                // 已接管
    // ⚠ 这里**不能**因为 disabled 就跳过。
    //   发票 / 采购单表单里的「合同」「分包商」下拉初始是 disabled ——
    //   要先选客户才由 JS 填上并启用。
    //   跳过它们的后果是：用户选完客户，控件**当场**从原生下拉
    //   变成搜索框。样式在交互中途变样是最难受的一类问题，
    //   而且「为什么这个下拉刚才没有搜索框」根本无从复现。
    //   正确做法是照样接管，并把 disabled 状态同步到按钮上。

    // flatpickr 的月份下拉：它有 12 项，会被阈值命中。
    // 三个判据任一成立都跳过 —— 全部用是因为历史上 flatpickr 改过版本，
    // 只靠 class 或只靠 tabindex 都可能漏。
    if (sel.classList.contains('flatpickr-monthDropdown-months')) return true;
    if (sel.getAttribute('aria-label') === 'Month') return true;
    if (sel.closest('.flatpickr-current-month, .flatpickr-calendar')) return true;
    if (sel.tabIndex === -1 && !sel.id && !sel.name) return true;

    // 动态表单（change / contract 选择）改 innerHTML 生成新 select 时，
    // 会在后面再扫一次；这里只挡「同一个 select 被扫两遍」。
    return sel.options.length < MIN_OPTIONS;
  }

  // --------------------------------------------------------------------------
  //  小工具
  // --------------------------------------------------------------------------
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  /** 归一化：全角空格、连续空白、去首尾。小写化留给匹配时做。 */
  function norm(s) {
    return String(s == null ? '' : s).replace(/　/g, ' ')
      .replace(/\s+/g, ' ').trim();
  }

  /** 二分/线性都行，选项最多两百多个，直接 filter。 */
  function match(opt, q) {
    if (!q) return true;
    // 同时匹配显示文本与 value —— 有些下拉 value 是编号，用户记得编号
    return norm(opt.textContent).toLowerCase().indexOf(q) >= 0
        || norm(opt.value).toLowerCase().indexOf(q) >= 0;
  }

  // --------------------------------------------------------------------------
  //  实例
  // --------------------------------------------------------------------------
  function Searchable(sel) {
    this.sel = sel;
    this.active = -1;          // 高亮项在 this.items 里的下标
    this.items = [];           // 当前**可见**的 option 元素
    this.build();
  }

  Searchable.prototype.build = function () {
    var self = this;
    var sel = this.sel;

    // --- 外壳 ---
    var wrap = el('div', 'ss');
    wrap.dataset.ss = '';
    wrap.__ss = this;                 // 反查实例用（disabled 变化时要重画按钮）
    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(sel);
    sel.classList.add('ss-native');
    sel.dataset.ssReady = '1';

    // --- 触发按钮 ---
    // 用 button 而不是 div：原生键盘（Enter/Space）免费就有，
    // 且不会被 `<label for>` 抢走点击（label 点的是 select，select 已被移走）。
    var btn = el('button', 'ss-toggle');
    btn.type = 'button';
    btn.setAttribute('aria-haspopup', 'listbox');
    btn.setAttribute('aria-expanded', 'false');
    var lab = el('span', 'ss-label');
    btn.appendChild(lab);
    var caret = el('i', 'bi bi-chevron-down ss-caret');
    btn.appendChild(caret);
    wrap.appendChild(btn);

    // --- 面板 ---
    var panel = el('div', 'ss-panel');
    panel.hidden = true;
    var search = el('input', 'ss-search');
    search.type = 'text';
    search.placeholder = '输入以筛选…';
    search.autocomplete = 'off';
    search.setAttribute('aria-label', '筛选选项');
    panel.appendChild(search);

    var list = el('div', 'ss-list');
    list.setAttribute('role', 'listbox');
    list.id = 'ss-list-' + (sel.id || this._uid());
    search.setAttribute('role', 'combobox');
    search.setAttribute('aria-expanded', 'true');
    search.setAttribute('aria-controls', list.id);
    search.setAttribute('aria-autocomplete', 'list');
    panel.appendChild(list);

    var empty = el('div', 'ss-empty');
    empty.hidden = true;
    panel.appendChild(empty);
    wrap.appendChild(panel);

    this.wrap = wrap; this.btn = btn; this.lab = lab;
    this.panel = panel; this.search = search;
    this.list = list; this.empty = empty;

    // 列表项：把每个 option 预先做成一个 div，选项少时直接建，
    // 几百项也就几百个节点（材料下拉 192 项，实测无卡顿）。
    this.built = false;

    this.syncLabel();
    this.bind();
  };

  /** 把当前选中项的文本写到按钮上，并同步 disabled 状态。 */
  Searchable.prototype.syncLabel = function () {
    var sel = this.sel;
    var opt = sel.selectedIndex >= 0 ? sel.options[sel.selectedIndex] : null;
    var text = opt ? norm(opt.textContent) : '';
    // 「第一个选项 value 为空」= 占位项（请选择客户 / 全部材料 / —）
    var isPlaceholder = opt && opt.value === '' && text !== '';
    this.lab.textContent = text || (sel.getAttribute('data-placeholder') || '请选择');
    this.wrap.classList.toggle('is-placeholder', !text || isPlaceholder);
    // disabled 要跟着原生走：页面 JS 会在选完客户后把 select 启用，
    // 按钮不同步的话就会变成「控件可点但点了没反应」。
    this.btn.disabled = !!sel.disabled;
  };

  /** 懒建列表项（第一次打开时才建）。 */
  Searchable.prototype.ensureList = function () {
    if (this.built) return;
    var self = this;
    var frag = document.createDocumentFragment();
    for (var i = 0; i < this.sel.options.length; i++) {
      var o = this.sel.options[i];
      var d = el('div', 'ss-opt', norm(o.textContent) || ('#' + (i + 1)));
      d.setAttribute('role', 'option');
      d.dataset.i = String(i);
      d.id = 'ss-opt-' + (this.sel.id || this._uid()) + '-' + i;
      if (o.disabled) { d.classList.add('is-disabled'); d.setAttribute('aria-disabled', 'true'); }
      frag.appendChild(d);
    }
    this.list.appendChild(frag);
    this.built = true;
  };

  Searchable.prototype._uid = function () {
    if (!this.__uid) this.__uid = 'x' + Math.floor(performance.now() * 1000);
    return this.__uid;
  };

  /** 按搜索词过滤并刷新高亮。 */
  Searchable.prototype.filter = function () {
    this.ensureList();
    var q = norm(this.search.value).toLowerCase();
    var opts = this.sel.options;
    var nodes = this.list.children;
    var shown = 0;
    for (var i = 0; i < opts.length; i++) {
      var node = nodes[i];
      if (!node) continue;
      var ok = match(opts[i], q);
      node.hidden = !ok;
      if (ok) {
        shown++;
        if (opts[i].selected) node.classList.add('is-selected');
        else node.classList.remove('is-selected');
      }
    }
    this.empty.hidden = shown !== 0;
    if (shown === 0) this.empty.textContent = '没有匹配「' + norm(this.search.value) + '」的选项';
    this.items = [];
    for (var k = 0; k < nodes.length; k++) if (!nodes[k].hidden) this.items.push(nodes[k]);
    this.active = -1;
  };

  /** 打开面板。 */
  Searchable.prototype.open = function () {
    if (openInstance && openInstance !== this) openInstance.close();
    openInstance = this;
    this.ensureList();
    this.panel.hidden = false;
    this.btn.setAttribute('aria-expanded', 'true');
    this.search.setAttribute('aria-expanded', 'true');
    this.search.value = '';
    this.filter();
    this.place();
    this.search.focus();
    document.addEventListener('mousedown', this._outside = this._onDocDown.bind(this), true);
    window.addEventListener('scroll', this._onScroll = this._reposition.bind(this), true);
    window.addEventListener('resize', this._onScroll);
  };

  Searchable.prototype.close = function (refocus) {
    if (this.panel.hidden) return;
    this.panel.hidden = true;
    this.btn.setAttribute('aria-expanded', 'false');
    this.search.setAttribute('aria-expanded', 'false');
    this.search.removeAttribute('aria-activedescendant');
    if (this._outside) { document.removeEventListener('mousedown', this._outside, true); this._outside = null; }
    if (this._onScroll) { window.removeEventListener('scroll', this._onScroll, true); this._onScroll = null; }
    window.removeEventListener('resize', this._onScroll);
    if (openInstance === this) openInstance = null;
    if (refocus) this.btn.focus();
  };

  Searchable.prototype._onDocDown = function (e) {
    if (!this.wrap.contains(e.target)) this.close();
  };

  Searchable.prototype._reposition = function () {
    if (!this.panel.hidden) this.place();
  };

  /**
   * 定位。用 fixed + 算视口空间，向上放不下就翻到按钮上方。
   * 不用 CSS 里的 `bottom:100%` 那种固定翻转 —— 视口高度是动态的。
   */
  Searchable.prototype.place = function () {
    var r = this.btn.getBoundingClientRect();
    var below = window.innerHeight - r.bottom;
    var above = r.top;
    var need = 44 + Math.min(MAX_LIST_H, this.items.length * 34) + 12;
    var up = below < need && above > below;
    this.panel.classList.toggle('ss-up', up);

    var top = up ? Math.max(8, r.top - 8) : r.bottom + 6;
    this.panel.style.top = top + 'px';
    // 宽度对齐按钮，但**不窄于 220px** —— 选项文本是公司名/项目名，太窄只能靠省略号。
    var w = Math.max(r.width, 220);
    var left = Math.min(r.left, window.innerWidth - w - 8);
    this.panel.style.left = Math.max(8, left) + 'px';
    this.panel.style.minWidth = w + 'px';
  };

  /** 移动高亮。dir = 1 下 / -1 上；null = 定位到当前选中项。 */
  Searchable.prototype.move = function (dir) {
    if (!this.items.length) return;
    var n = this.items.length;
    var i;
    if (dir === null) {
      i = this.items.findIndex(function (n) { return n.classList.contains('is-selected'); });
      if (i < 0) i = 0;
    } else if (dir === 'first') { i = 0; }
    else if (dir === 'last') { i = n - 1; }
    else { i = this.active < 0 ? (dir > 0 ? -1 : 0) : this.active + dir; }
    if (i < 0) i = 0;
    if (i >= n) i = n - 1;

    if (this.active >= 0 && this.items[this.active]) {
      this.items[this.active].classList.remove('is-active');
    }
    this.active = i;
    var node = this.items[i];
    node.classList.add('is-active');
    // 列表要跟着滚
    if (node.offsetTop < this.list.scrollTop) this.list.scrollTop = node.offsetTop - 4;
    else if (node.offsetTop + node.offsetHeight > this.list.scrollTop + this.list.clientHeight) {
      this.list.scrollTop = node.offsetTop + node.offsetHeight - this.list.clientHeight + 4;
    }
    // aria-activedescendant 挂在**搜索框**上：焦点始终在输入框，
    // 键盘用户靠这个属性知道「屏幕阅读器该念哪一项」。
    // 触发按钮上那份是为了闭合模式下也有一条可用路径。
    this.search.setAttribute('aria-activedescendant', node.id);
  };

  Searchable.prototype.pick = function (node) {
    if (!node || node.classList.contains('is-disabled')) return;
    var i = +node.dataset.i;
    this.sel.selectedIndex = i;
    this.syncLabel();
    this.close(true);
    // ⚠ 必须派发 change：筛选栏里那些 select 靠 onchange 自动提交表单
    //（fltSubmit / po-status 那类）。不派发 = 选了值但页面不刷新。
    this.sel.dispatchEvent(new Event('change', { bubbles: true }));
  };

  Searchable.prototype.bind = function () {
    var self = this;

    this.btn.addEventListener('click', function () {
      if (self.panel.hidden) self.open(); else self.close(true);
    });

    this.search.addEventListener('input', function () { self.filter(); });

    this.search.addEventListener('keydown', function (e) {
      switch (e.key) {
        case 'ArrowDown': e.preventDefault(); self.move(1); break;
        case 'ArrowUp': e.preventDefault(); self.move(-1); break;
        case 'Home': e.preventDefault(); self.move('first'); break;
        case 'End': e.preventDefault(); self.move('last'); break;
        case 'Enter':
          e.preventDefault();
          if (self.active < 0) self.move(null);
          self.pick(self.items[self.active]);
          break;
        case 'Escape': e.preventDefault(); e.stopPropagation(); self.close(true); break;
        case 'Tab': self.close(); break;   // 让焦点自然往下走
        default: break;
      }
    });

    this.list.addEventListener('mousedown', function (e) {
      // mousedown 而不是 click：否则输入框先失焦，blur 里 close 掉面板，
      // click 就落在已经 display:none 的节点上，选项永远选不中。
      var n = e.target.closest('.ss-opt');
      if (n) { e.preventDefault(); self.pick(n); }
    });

    this.list.addEventListener('mousemove', function (e) {
      var n = e.target.closest('.ss-opt');
      if (!n || n.classList.contains('is-active')) return;
      if (self.active >= 0 && self.items[self.active]) self.items[self.active].classList.remove('is-active');
      self.active = self.items.indexOf(n);
      n.classList.add('is-active');
    });

    // 关闭状态下：↑↓ Home End 直接换值，字母跳到首项 —— 与原生 select 一致
    this.btn.addEventListener('keydown', function (e) {
      var opts = self.sel.options;
      if (!opts.length) return;
      var n = opts.length, i = self.sel.selectedIndex;
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        var d = e.key === 'ArrowDown' ? 1 : -1;
        var j = i;
        for (var t = 0; t < n; t++) {
          j += d;
          if (j < 0) j = n - 1;
          if (j >= n) j = 0;
          if (!opts[j].disabled) break;
        }
        self.sel.selectedIndex = j;
        self.syncLabel();
        self.sel.dispatchEvent(new Event('change', { bubbles: true }));
      } else if (e.key === 'Home' || e.key === 'End') {
        e.preventDefault();
        self.sel.selectedIndex = e.key === 'Home' ? 0 : n - 1;
        self.syncLabel();
        self.sel.dispatchEvent(new Event('change', { bubbles: true }));
      } else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
        e.preventDefault(); self.open();
      }
    });
  };

  // --------------------------------------------------------------------------
  //  扫描与初始化
  // --------------------------------------------------------------------------
  function enhanceOne(sel) {
    if (shouldSkip(sel)) return false;
    try {
      new Searchable(sel);
      return true;
    } catch (e) {
      // 增强失败必须**把原生 select 放回去**，否则这个字段直接没法用。
      //   catch 之后没有 rollback = 一次 JS 异常让表单少一个字段。
      sel.classList.remove('ss-native');
      delete sel.dataset.ssReady;
      if (window.console) console.error('[searchable-select] 增强失败，已回退原生', sel, e);
      return false;
    }
  }

  function scan(root) {
    var scope = root || document;
    var list = (scope.matches && scope.matches('select'))
      ? [scope] : scope.querySelectorAll('select');
    for (var i = 0; i < list.length; i++) enhanceOne(list[i]);
  }

  /**
   * 动态填充的 select 必须能后补。
   *
   * 真实场景：发票 / 采购单 / 材料表单里的「合同」下拉是
   * **先出页面、JS 再填**的（/invoices/new 的 contract_id 有 49 项）。
   * DOMContentLoaded 那一刻它只有 1 个占位项 —— 8 这个阈值判定它
   * 「不需要搜索」，于是永远不会被接管。
   *
   * 症状很有欺骗性：同一个页面上「客户」下拉有搜索框、「合同」下拉没有，
   * 而两者的选项数一个 10 一个 49，看起来像随机的不一致，
   * 很难让人联想到「填充时机晚于扫描时机」。
   *
   * 所以挂一个 MutationObserver：
   *   · 只看**新增的 select**（不重扫全文档 —— 甘特图那种高频 DOM 变动扛不住）
   *   · 跳过 .ss 内部（那是我自己插进去的节点，否则会自激）
   *   · 去抖 80ms：填 49 个 option 是连续多次 DOM 变更
   */
  function watch(root) {
    if (!window.MutationObserver) return;
    var timer = null;
    var obs = new MutationObserver(function (muts) {
      var fresh = [];
      muts.forEach(function (m) {
        // ⚠ 两种填充方式要分开处理，只看 addedNodes 会漏掉其中一种：
        //   (a) `div.innerHTML = '<select>...'`  → addedNodes 里有 <select>
        //   (b) `sel.innerHTML = '<option>...'` → addedNodes 里只有 <option>，
        //       <select> 是 m.target。第一版只处理 (a)，
        //       于是 /invoices/new 的「合同」下拉（49 项，事后填 option）
        //       永远接管不了 —— 而同页的「客户」下拉（10 项，初始就有）接管了，
        //       两者不一致看起来像随机，很难查。
        if (m.target && m.target.nodeType === 1 && m.target.tagName === 'SELECT') {
          // 已接管的：多半是 disabled 被页面 JS 打开了，重画按钮即可
          var w = m.target.closest ? m.target.closest('.ss') : null;
          if (w && w.__ss) w.__ss.syncLabel();
          else fresh.push(m.target);
        }
        Array.prototype.forEach.call(m.addedNodes || [], function (n) {
          if (n.nodeType !== 1) return;
          if (n.closest && n.closest('.ss')) return;      // 我自己插的，忽略
          if (n.tagName === 'SELECT') { fresh.push(n); return; }
          if (n.querySelectorAll) {
            Array.prototype.push.apply(fresh, n.querySelectorAll('select'));
          }
        });
      });
      if (!fresh.length) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () {
        timer = null;
        fresh.forEach(enhanceOne);
      }, 80);
    });
    // attributeFilter 只看 disabled：页面 JS 会在选完客户后把它打开，
    // 按钮要跟着解禁，否则就是「控件看着能点、点了没反应」。
    // 其余属性变化（class/style/aria）不管 —— 那些不影响可用性，
    // 全监听会在高频改样式的页面上把这里变成性能瓶颈。
    obs.observe(root || document.body, {
      childList: true, subtree: true,
      attributes: true, attributeFilter: ['disabled']
    });
  }

  // 供动态表单用：材料/合同联动会 innerHTML 生成新 select。
  window.SearchableSelect = {
    scan: scan,
    MIN_OPTIONS: MIN_OPTIONS,
    instanceOf: function (s) { return !!(s.closest && s.closest('.ss')); }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      scan();
      watch(document.body);
    });
  } else {
    scan();
    watch(document.body);
  }
})();
