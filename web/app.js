/* app.js —— Token 用量统计 · 界面逻辑
 *
 * 口径不在这里实现，全部委托 token-semantics.js（与 Python 参考实现等价，
 * 已用 Node 对照内置基准验证 81/81 天完全一致）。
 * 本文件只负责：选文件 → 筛选 → 渲染。
 */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var DAY_W = 110;          // 趋势图每天像素宽
  /** 系统是否处于深色模式（配色随之切换）。 */
  function isDarkMode() {
    return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  }

  var HEAT_TOP = 24;        // 热力图模型数（字号放大后可上下滑动，多留一些）
  var HEAT_ROWS = 10;       // 纵向可视行数，其余靠滚动
  var HEAT_LABEL_W = 276;   // 首列宽（须与 index.html 的 .c-label 一致）
  var HEAT_TOTAL_W = 88;    // 末列宽
  var HEAT_MIN_W = 56;      // 色块最小列宽

  // ------------------------------------------------------------------ 格式化
  function fmtCompact(n) {
    n = n || 0;
    if (n >= 1e8) return (n / 1e8).toFixed(2) + ' 亿';
    if (n >= 1e4) return (n / 1e4).toFixed(1) + ' 万';
    return String(Math.round(n));
  }
  function fmtInt(n) { return Math.round(n || 0).toLocaleString('en-US'); }
  function fmtPct(x) { return (x * 100).toFixed(1) + '%'; }
  function fmtMoney(v) {
    if (!isFinite(v) || v <= 0) return '—';
    if (v >= 10000) return '¥' + (v / 10000).toFixed(2) + ' 万';
    return '¥' + v.toFixed(2);
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  /** 拆解 agent 为「客户端 / 是否自有 Key / 纯模型名」。
   *  agent 形如 workbuddy-model:custom-local:glm-5.3 或 codex-model:gpt-6-luna */
  function sourceOf(agent) {
    var i = agent.indexOf('-model:');
    if (i < 0) return { client: 'other', label: '其他', name: agent, custom: false };
    var client = agent.slice(0, i);
    var rid = agent.slice(i + 7);
    var custom = /^custom-local:/i.test(rid);
    var name = custom ? rid.slice('custom-local:'.length) : rid;
    var label = client === 'workbuddy' ? (custom ? '自定义 API' : 'WorkBuddy')
      : client === 'codex' ? 'Codex'
      : client === 'cursor' ? 'Cursor'
      : client === 'deepseek' ? 'DeepSeek'
      : client === 'claude' ? 'Claude Code'
      : client;
    return { client: client, label: label, name: name, custom: custom };
  }

  /** 徽标类名：颜色在 CSS 里按明暗两套定义，这里只决定用哪一类。 */
  function badgeClass(agent) {
    var s = sourceOf(agent);
    return 'b-' + (s.client === 'workbuddy' ? (s.custom ? 'byok' : 'workbuddy')
      : s.client === 'codex' ? 'codex'
      : s.client === 'cursor' ? 'cursor'
      : s.client === 'deepseek' ? 'dsh'
      : 'other');
  }

  function sourceBadge(agent) {
    return '<span class="badge ' + badgeClass(agent) + '">' + esc(sourceOf(agent).label) + '</span>';
  }

  function shortModel(agent) {
    return sourceOf(agent).name;
  }

  /** 已下架模型标记（官方文档明确退役的型号）。 */
  function retiredBadge(agent) {
    var T = window.WB_PRICES || {};
    var nm = sourceOf(agent).name.toLowerCase();
    var hit = (T.retired || {})[nm];
    if (!hit) return '';
    return '<span class="badge b-retired" title="' + esc(hit) + '">已下架</span>';
  }
  function niceCeil(v) {
    if (v <= 0) return 1;
    var exp = Math.floor(Math.log10(v));
    var base = Math.pow(10, exp);
    var m = v / base;
    var step = m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10;
    return step * base;
  }
  function lerpColor(c1, c2, t) {
    return 'rgb(' + c1.map(function (v, i) {
      return Math.round(v + (c2[i] - v) * t);
    }).join(',') + ')';
  }

  // ------------------------------------------------------------------ 路径识别
  function filePath(f) { return f.__path || f.webkitRelativePath || f.name || ''; }
  /** 子代理会话：<项目>/<会话UUID>/subagents/agent-*.jsonl
   *  HUD 会登记这些文件但不导入其用量（实测 62 个事件时间戳在账本中命中 0 次）。 */
  function isSubagentFile(f) { return /\/subagents\//i.test(filePath(f)); }
  function projectDir(p) {
    var seg = String(p).split('/').filter(Boolean);
    if (seg.length < 2) return '(未知)';
    var parent = seg[seg.length - 2];
    if (parent === 'subagents') return seg[seg.length - 4] || parent;
    return parent;
  }

  // ------------------------------------------------------------------ 状态
  var ACC = [];            // 已选文件（追加）
  var EVENTS = [];         // 全量事件
  var COVER = {};          // 项目目录 → {files, events, token}
  var META = { files: 0, skipped: 0, ms: 0, subExcluded: 0, subIncluded: 0 };
  // 时间范围按分类各存一份：切分类时不互相影响，各自记住自己的选择
  var RANGES = Object.create(null);
  function rangeOf(cat) {
    if (!RANGES[cat]) RANGES[cat] = { mode: 'all', from: null, to: null };
    return RANGES[cat];
  }
  var SEL_SESSION = null;
  var HOUR_DATA = [];       // 半小时桶的明细，供悬停提示使用
  var TITLES = Object.create(null);    // sessionId → 会话名称（ai-title）
  var PREVIEWS = Object.create(null);  // sessionId → 首条用户消息摘要（兜底）

  function sessionLabel(sid) {
    if (sid === WBToken.UNKNOWN_SESSION) return '(无会话标识)';
    return TITLES[sid] || PREVIEWS[sid] || sid;
  }
  var CUR = null;          // 当前区间的聚合结果（改单价时局部重渲染，避免整页刷新）
  var COSTS = null;        // 当前区间的成本汇总（逐事件计价）

  // 单价表由 prices.js 提供；引擎初始化失败时成本功能整体降级，不影响其余统计
  var COST_OK = false;
  (function initCost() {
    try {
      if (window.WBPrice && window.WB_PRICES) COST_OK = window.WBPrice.init(window.WB_PRICES);
    } catch (e) {
      console.warn('成本引擎初始化失败', e);
      COST_OK = false;
    }
  })();

  /** 币种感知的金额格式；小额保留 4 位，避免显示成 0.00。 */
  function fmtMoney(v, cur) {
    if (v == null || !isFinite(v)) return '—';
    var sym = cur === 'CNY' ? '¥' : cur === 'USD' ? '$' : '';
    var abs = Math.abs(v);
    var num;
    if (abs >= 10000) num = (v / 10000).toFixed(2) + ' 万';
    else if (abs > 0 && abs < 0.01) num = v.toFixed(4);
    else num = v.toFixed(2);
    return sym + num + (sym ? '' : ' ' + (cur || ''));
  }

  // ------------------------------------------------------------------ 文件选择
  function entryFiles(entry, out) {
    return new Promise(function (resolve) {
      if (!entry) return resolve();
      if (entry.isFile) {
        entry.file(function (f) {
          try { f.__path = entry.fullPath; } catch (e) { /* File 不可扩展时忽略 */ }
          out.push(f);
          resolve();
        }, function () { resolve(); });
        return;
      }
      if (entry.isDirectory) {
        var reader = entry.createReader();
        var all = [];
        (function batch() {
          reader.readEntries(function (entries) {
            if (!entries.length) {
              Promise.all(all.map(function (e) { return entryFiles(e, out); })).then(resolve);
              return;
            }
            all = all.concat(Array.prototype.slice.call(entries));
            batch();
          }, function () { resolve(); });
        })();
        return;
      }
      resolve();
    });
  }

  var drop = $('drop'), fileMulti = $('fileMulti'), fileDir = $('fileDir');
  var progressBox = $('progress'), barFill = $('barFill'), pstat = $('pstat');
  var resultBox = $('result'), clearBtn = $('clearBtn');
  var lastPick = 0;

  drop.addEventListener('click', function (e) {
    if (e.target.closest('button') || e.target.closest('label')) return;
    if (Date.now() - lastPick < 400) return;
    fileMulti.click();
  });
  $('pickFolder').addEventListener('click', function (e) { e.stopPropagation(); fileDir.click(); });
  $('pickFiles').addEventListener('click', function (e) {
    e.stopPropagation();
    lastPick = Date.now();
    fileMulti.click();
  });
  clearBtn.addEventListener('click', function () {
    ACC = []; EVENTS = []; COVER = {}; SEL_SESSION = null;
    resultBox.classList.add('hide');
    progressBox.classList.remove('on');
    clearBtn.disabled = true;
  });
  ['dragenter', 'dragover'].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add('over'); });
  });
  ['dragleave', 'drop'].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove('over'); });
  });
  drop.addEventListener('drop', async function (e) {
    var items = e.dataTransfer.items, files = [];
    if (items && items.length && items[0].webkitGetAsEntry) {
      var entries = [];
      for (var i = 0; i < items.length; i++) {
        var en = items[i].webkitGetAsEntry && items[i].webkitGetAsEntry();
        if (en) entries.push(en);
      }
      for (var j = 0; j < entries.length; j++) await entryFiles(entries[j], files);
    } else {
      files = Array.prototype.slice.call(e.dataTransfer.files);
    }
    ingest(files);
  });
  fileMulti.addEventListener('change', function () { ingest(this.files); this.value = ''; });
  fileDir.addEventListener('change', function () { ingest(this.files); this.value = ''; });
  $('includeSub').addEventListener('change', function () {
    if (SRC === 'server') { loadFromServer(); return; }
    if (ACC.length) ingest(null);
  });

  // ------------------------------------------------------------------ 解析
  async function ingest(newFiles) {
    if (newFiles) {
      var add = Array.prototype.slice.call(newFiles).filter(function (f) {
        return /\.jsonl$/i.test(f.name);
      });
      if (!add.length) { alert('没找到 .jsonl 文件'); return; }
      ACC = ACC.concat(add);
    }
    if (!ACC.length) return;

    var includeSub = $('includeSub').checked;
    var subs = ACC.filter(isSubagentFile);
    var files = includeSub ? ACC : ACC.filter(function (f) { return !isSubagentFile(f); });
    if (!files.length) { alert('过滤后没有可统计的会话文件'); return; }

    clearBtn.disabled = false;
    progressBox.classList.add('on');
    barFill.style.width = '0%';
    resultBox.classList.add('hide');

    TITLES = Object.create(null);
    PREVIEWS = Object.create(null);
    var totalBytes = files.reduce(function (s, f) { return s + f.size; }, 0);
    var done = 0, seen = new Set(), events = [], skipped = 0;
    COVER = {};
    var t0 = performance.now();

    for (var i = 0; i < files.length; i++) {
      var f = files[i];
      var sid = f.name.replace(/\.jsonl$/i, '');
      pstat.textContent = '解析 ' + (i + 1) + '/' + files.length + ' · ' + f.name;

      if (seen.has(sid)) { skipped++; done += f.size; continue; }
      seen.add(sid);

      var pj = projectDir(filePath(f));
      if (!COVER[pj]) COVER[pj] = { files: 0, events: 0, token: 0 };
      COVER[pj].files += 1;

      try {
        await WBToken.forEachLine(f, function (line) {
          var ev = WBToken.parseTranscriptLine(line, sid);
          if (ev) {
            events.push(ev);
            COVER[pj].events += 1;
            COVER[pj].token += ev.tin + ev.cread + ev.tout;
            return;
          }
          // 非用量行：顺带抓会话名称（两个函数内部都有子串快速过滤，开销可忽略）
          if (!TITLES[sid]) {
            var t = WBToken.parseTitleLine(line);
            if (t) TITLES[sid] = t;
            else if (!PREVIEWS[sid]) {
              var pv = WBToken.parseUserPreview(line);
              if (pv) PREVIEWS[sid] = pv;
            }
          }
        });
      } catch (err) {
        console.warn('读取失败', f.name, err);
      }

      done += f.size;
      barFill.style.width = (done / totalBytes * 100).toFixed(1) + '%';
      await new Promise(function (r) { setTimeout(r, 0); });
    }

    META = {
      files: files.length,
      skipped: skipped,
      ms: Math.round(performance.now() - t0),
      subExcluded: includeSub ? 0 : subs.length,
      subIncluded: includeSub ? subs.length : 0,
      subMode: includeSub ? 'included' : 'excluded'
    };
    EVENTS = events;

    pstat.textContent = '完成：' + files.length + ' 个文件 · ' + fmtInt(events.length) +
      ' 次调用 · 用时 ' + (META.ms / 1000).toFixed(1) + ' 秒';

    RANGES = Object.create(null);
    SEL_SESSION = null;
    syncRangeButtons();
    initDateInputs();
    renderAll();
  }

  // ------------------------------------------------------------------ 额度与积分
  var QUOTA = null;
  // 客户端/厂商显示名。键有两种来源：HUD 账本的 scope 原名（小写，无报告时
  // 的退路），以及 HUD 报告 accounts 里的 client 名（"GLM" / "GitHub Copilot"）。
  // GLM 一项是本机实证：open-agents scope 的 pool 窗口经哈希关联到报告的
  // accounts[WorkBuddy]（label = "GLM · CN · PLAN · 凭据 f824d6"），
  // 即用户在 WorkBuddy 里通过 API 接入的智谱 Coding Plan。
  var QLABEL = { codex: 'Codex', claude: 'Claude Code', deepseek: 'DeepSeek',
                 cursor: 'Cursor', copilot: 'GitHub Copilot',
                 'open-agents': 'Open Agents', workbuddy: 'WorkBuddy',
                 GLM: '智谱 GLM Coding Plan', 'GitHub Copilot': 'GitHub Copilot',
                 Cursor: 'Cursor', Codex: 'Codex' };

  /** 秒级时间戳 → "3 小时后" / "2 天后"；过去则返回 ""。 */
  function fmtUntil(sec) {
    if (!sec) return '';
    var diff = sec - Date.now() / 1000;
    if (diff <= 0) return '';
    if (diff < 3600) return Math.max(1, Math.round(diff / 60)) + ' 分钟后';
    if (diff < 86400) return Math.round(diff / 3600) + ' 小时后';
    return Math.round(diff / 86400) + ' 天后';
  }
  function fmtWhen(sec) {
    if (!sec) return '';
    var d = new Date(sec * 1000);
    return (d.getMonth() + 1) + '-' + ('0' + d.getDate()).slice(-2) + ' ' +
           ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
  }

  /**
   * 一行额度。两种形态：
   *   · 百分比型（多数套餐）→ 进度条 + 「剩余 xx%」
   *   · 余额型（Kimi 开放平台等按量计费）→ 直接显示金额，没有"剩余百分比"这回事
   * stampAt 为这一行的读数时间（HUD 观测时间 / 官方接口读取时间）。
   */
  function qWindowRow(w, stampAt) {
    // 数值型（积分这类没有百分比、也不是钱的量）：只显示数字。
    // 默认紧凑写法（万/亿）；需要精确读数时传 exact 走千分位。
    if (w.count !== undefined && w.count !== null) {
      var ntxt = w.exact
        ? (w.decimals ? Number(w.count).toFixed(w.decimals) : fmtInt(w.count))
        : fmtCompact(w.count);
      return '<div class="qrow"><div class="ql"><span>' + esc(w.label) + '</span><b>' +
             ntxt + '</b></div>' +
             (stampAt ? '<div class="qmeta">更新于 ' + fmtWhen(stampAt) + '</div>' : '') + '</div>';
    }
    if (w.balance !== undefined && w.balance !== null) {
      var extra = [];
      if (w.voucher !== undefined && w.voucher !== null) {
        extra.push('代金券 ' + fmtMoney(w.voucher, w.currency));
      }
      if (w.cash !== undefined && w.cash !== null) {
        extra.push('现金 ' + fmtMoney(w.cash, w.currency));
      }
      return '<div class="qrow"><div class="ql"><span>' + esc(w.label) + '</span><b>' +
             fmtMoney(w.balance, w.currency) + '</b></div>' +
             (extra.length ? '<div class="qmeta">' + esc(extra.join(' · ')) + '</div>' : '') +
             '<div class="qmeta">更新于 ' + fmtWhen(stampAt) + '</div></div>';
    }
    var rem = Math.max(0, Math.min(100, w.remaining_pct));
    var cls = rem >= 50 ? 'ok' : (rem >= 20 ? 'mid' : 'low');
    // 原始窗口 ID 是厂商/账本内部标识（含哈希），对用户无意义，不再挂 title
    return '<div class="qrow"><div class="ql"><span>' + esc(w.label) +
           '</span><b>剩余 ' + rem.toFixed(0) + '%</b></div>' +
           '<div class="qbar ' + cls + '"><i style="width:' + rem + '%"></i></div>' +
           '<div class="qmeta">' +
           (w.resets_at ? fmtUntil(w.resets_at) + '重置（' + fmtWhen(w.resets_at) + '）· ' : '') +
           '更新于 ' + fmtWhen(stampAt) + '</div></div>';
  }

  function renderQuota() {
    if (!QUOTA) {
      // 文件模式（手动选文件夹）没有本地服务，取不到额度/积分
      $('quotaBox').innerHTML = '<p class="qnote">额度与积分需要应用在运行（当前为文件模式），' +
        '此时仅统计已选文件夹内的用量。</p>';
      return;
    }
    var groups = QUOTA.quota || [];
    // cards 用 {k, h} 结构：k 是排序键，用来把 WorkBuddy 积分卡精确插到 Kimi 之后
    var cards = [], notes = '';
    groups.forEach(function (g) {
      var title = QLABEL[g.client] || g.client;
      // 副标题：账号标识（脱敏邮箱/凭据名）+ 数据来源。
      // 「本机日志」比「rollout」更适合给普通用户看。
      var srcTag = (g.sources || []).indexOf('codex-rollout') >= 0 ? '本机日志' : '本地记录';
      var sub = (g.account || '') + ' · ' + srcTag;
      var parts = ['<div class="qcard"><div class="qt"><span class="qt-t">' + esc(title) +
                   '</span>' +
                   (g.plan ? ' <span class="badge b-other">' + esc(g.plan) + '</span>' : '') +
                   '</div><div class="qs">' + esc(sub) + '</div>'];
      g.windows.forEach(function (w) {
        parts.push(qWindowRow(w, w.observed_at));
      });
      parts.push('</div>');
      cards.push({ k: 'grp:' + g.client, h: parts.join('') });
    });

    // 外部套餐（API 接入的 Coding Plan）：**有额度窗口的直接并入上面这组卡片，
    // 不再单独分区**——用户要求"只要应用本身能区分开，显示就不区分"。
    // 只有拿不到窗口的（未开启/读取失败）才需要说明文字。
    (QUOTA.plans || []).forEach(function (p) {
      if (p.windows && p.windows.length) {
        var sub = (p.model_name ? p.model_name + ' · ' : '') + (p.gateway || '');
        var parts = ['<div class="qcard"><div class="qt"><span class="qt-t">' + esc(p.name) +
                     '</span>' +
                     (p.plan ? ' <span class="badge b-codex">' + esc(p.plan) + '</span>' : '') +
                     (p.status === 'probe' ? ' <span class="badge b-other">规则待确认</span>' : '') +
                     '</div><div class="qs">' + esc(sub) + '</div>'];
        p.windows.forEach(function (w) {
          parts.push(qWindowRow(w, p.fetched_at));
        });
        parts.push('</div>');
        cards.push({ k: 'plan:' + p.probe_id, h: parts.join('') });
      } else {
        // 拿不到窗口的套餐：失败说原因，未开启说方法，被 HUD 覆盖说清关系
        var msg = p.error ? '额度读取失败：' + p.error
          : (p.note || (!QUOTA.plan_quota_enabled
             ? '额度查询未开启。' + (QUOTA.plan_quota_how || '') : '本次未返回额度窗口。'));
        if (p.status === 'probe' && !p.error) {
          msg = '该套餐暂不支持读取额度（读取规则未验证）。' + msg;
        }
        notes += '<p class="qnote">' + esc(p.name) + '（' + esc(p.gateway || '') + '）：' +
                 esc(msg) + '</p>';
      }
    });

    // WorkBuddy 积分：额度网格里的一张卡（不另设明细区）。放在 Kimi 之后。
    // 口径：credit_json 里逐次调用的真实积分消耗；不用 session_usage.used
    // （那是上下文 token 占用，2026-10-23 已校正，见 wb_credits.py 文件头）。
    var crb = (QUOTA && QUOTA.credits_brief) || {};
    if (crb.available) {
      // 两行数值，样式对齐概览 KPI：数字放大、说明在右侧小号浅色。
      // 有「已获得/总额」时显示 已用/总额；本地拿不到分母就只显示消耗量，
      // 不拿 0 或占位符冒充（账户额度在服务端，本地确实没有）。
      var mGrant = crb.credit_this_month_granted;   // 预留：服务端提供后自动生效
      var tGrant = crb.credit_granted;
      var monthTxt = mGrant ? (fmtInt(crb.credit_this_month) + ' / ' + fmtInt(mGrant))
                            : fmtInt(crb.credit_this_month);
      var totalTxt = tGrant ? (fmtInt(crb.total_credit) + ' / ' + fmtInt(tGrant))
                            : fmtInt(crb.total_credit);
      var monthLbl = mGrant ? '本月积分消耗 / 本月已获得' : '本月积分消耗';
      var totalLbl = '总积分消耗' + (crb.first_month ? '（' + crb.first_month + ' 起）' : '');
      if (tGrant) totalLbl = '总积分消耗 / 总获得积分';
      var crCard = '<div class="qcard"><div class="qt"><span class="qt-t">WorkBuddy</span>' +
        ' <span class="badge b-other">积分</span></div>' +
        '<div class="qs">' + fmtInt(crb.calls) + ' 次调用 · ' +
        fmtInt(crb.sessions_billed) + ' 个会话有积分记录</div>' +
        '<div class="qstat"><b>' + monthTxt + '</b><span>' + esc(monthLbl) + '</span></div>' +
        '<div class="qstat"><b>' + totalTxt + '</b><span>' + esc(totalLbl) + '</span></div>' +
        '<div class="qnote">仅统计消耗积分的调用（自带 API Key 的模型不计量）；' +
        '账户余额与到期情况由服务端管理。</div></div>';
      var ki = -1;
      for (var ci = 0; ci < cards.length; ci++) {
        if (cards[ci].k === 'plan:kimi-openapi') ki = ci;
      }
      cards.splice(ki >= 0 ? ki + 1 : cards.length, 0, { k: 'wb-credits', h: crCard });
    }

    // 已登记但缺凭据的渠道：明确告知怎么补，而不是静默不显示
    (QUOTA.plans_missing || []).forEach(function (m) {
      notes += '<p class="qnote">' + esc(m.name) + '：' + esc(m.hint || '') + '</p>';
    });

    // 本地确实拿不到的数据：一句话说清边界，避免用户以为漏读
    notes += '<p class="qnote">暂不支持：Claude Code 额度（本机无可用额度数据）。</p>';

    // 顶部：厂商接口直连开关（应用内直接切换，用户不必去改配置文件）
    var pqOn = !!(QUOTA && QUOTA.plan_quota_enabled);
    var head = '';
    if (SRC === 'server') {
      head = '<div class="qhead"><span class="qk">厂商额度查询</span>' +
        '<button class="sm" id="planQuotaBtn">' +
        (pqOn ? '已开启 · 点击关闭' : '未开启 · 点击开启') + '</button>' +
        '<span class="qm" id="planQuotaHint">' + (pqOn
          ? '正在向厂商查询额度与余额，使用本机已配置的 API Key，数据不离开本机'
          : '开启后可读取智谱、Kimi 等套餐的额度与账户余额') + '</span></div>';
    }

    var html = head + (cards.length
      ? '<div class="qgrid">' + cards.map(function (c) { return c.h; }).join('') + '</div>'
      : '<p class="empty">未检测到额度数据。可在上方开启「厂商额度查询」读取。</p>');
    html += notes;
    $('quotaBox').innerHTML = html;

    // 未登记额度端点的网关：独立分区（用量已统计，缺的只是额度显示）
    var un = (QUOTA && QUOTA.unmatched_gateways) || [];
    if (un.length) {
      html = '<div class="qsub">已接入但暂不支持额度显示的外部 API</div>' +
        '<p class="qnote" style="margin-top:0">这些服务的 token 用量已计入上方统计；' +
        '余额与额度暂不支持读取。</p>' +
        tblSplit('<col style="width:46%"><col style="width:54%">',
                 '<th>服务地址</th><th>使用中的模型</th>',
                 un.map(function (u) {
                   return '<tr><td class="cname" title="' + esc(u.gateway) + '">' +
                          esc(u.gateway) + '</td><td class="cname" title="' +
                          esc(u.model_name) + '">' + esc(u.model_name || '—') + '</td></tr>';
                 }).join(''), 0, 'ctab');
      $('quotaBox').innerHTML += html;
    }

    // 联网行（默认关闭；开启后由用户自填端点拉取）
    var on = QUOTA.online || {};
    if (on.enabled && (on.rows || []).length) {
      var oh = '<div class="qgrid">';
      on.rows.forEach(function (r) {
        oh += '<div class="qcard"><div class="qt">' + esc(QLABEL[r.client] || r.client) +
              ' · ' + esc(r.label) + '</div><div class="qs">联网读取 · ' +
              fmtWhen(r.fetched_at) + '</div>' +
              '<div class="csum"><span class="n">' + fmtCompact(r.value) + '</span>' +
              '<span class="u">' + esc(r.unit || '') + '</span></div></div>';
      });
      oh += '</div>';
      $('quotaBox').innerHTML += oh;
    }
    // 自动刷新说明：分清「哪些数据在动、哪些是快照」，避免用户以为全局实时
    if (SRC === 'server') {
      var stamp = QUOTA.generated_at ? ' · 上次读取 ' + esc(QUOTA.generated_at) : '';
      $('quotaBox').innerHTML += '<p class="qnote">额度与积分每 ' +
        (QUOTA_REFRESH_MS / 1000) + ' 秒自动刷新（切到后台时暂停）' + stamp +
        '；用量与成本为打开页面或手动刷新时的快照。</p>';
    }

    // 开关的点击处理：POST 到本机服务，改一个布尔后立即重取额度
    var pqBtn = $('planQuotaBtn');
    if (pqBtn && pqBtn.addEventListener) {
      pqBtn.addEventListener('click', function () {
        var next = !(QUOTA && QUOTA.plan_quota_enabled);
        pqBtn.disabled = true;
        pqBtn.textContent = next ? '正在开启…' : '正在关闭…';
        fetch('/api/plan-quota', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: next })
        }).then(function (r) { return r.json(); }).then(function (res) {
          if (!res || !res.ok) throw new Error((res && (res.message || res.error)) || '切换失败');
          return refreshQuota();
        }).catch(function (e) {
          pqBtn.disabled = false;
          pqBtn.textContent = '切换失败 · 点击重试';
          var hint = $('planQuotaHint');
          if (hint) hint.textContent = '切换失败：' + (e.message || e);
        });
      });
    }
  }

  // ------------------------------------------------------------------ 额度自动刷新
  /** 自动刷新间隔：60 秒，取同类工具的事实标准（dsh 的额度插件默认 refreshMs=60000）。
   *
   *  两个刻意的设计：
   *  ① 只刷 /api/quota（轻量：读 sqlite + 少量 HTTP），**不刷 /api/events**——
   *     后者每次要遍历 400MB transcripts，每分钟跑会持续占用磁盘；
   *  ② 页面不可见时暂停（document.hidden），切回前台立刻补一次——
   *     用户看到的永远是当前值，后台标签页也不白耗电。 */
  var QUOTA_REFRESH_MS = 60000;
  var _quotaTimer = null;

  function refreshQuota() {
    return fetch('/api/quota', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (q) {
        if (!q) return;
        QUOTA = q;
        renderQuota();
      })
      .catch(function () { /* 服务重启瞬断等情况下静默跳过，下一周期再试 */ });
  }

  function startQuotaAutoRefresh() {
    stopQuotaAutoRefresh();
    if (SRC !== 'server') return;          // 文件模式没有 /api/quota
    _quotaTimer = setInterval(function () {
      if (document.hidden) return;
      refreshQuota();
    }, QUOTA_REFRESH_MS);
    document.addEventListener('visibilitychange', onVisibilityChange);
  }

  function stopQuotaAutoRefresh() {
    if (_quotaTimer) { clearInterval(_quotaTimer); _quotaTimer = null; }
  }

  function onVisibilityChange() {
    if (!document.hidden && SRC === 'server') refreshQuota();
  }
  // ------------------------------------------------------------------ 时间范围
  var SRC_LABELS = { workbuddy: 'WorkBuddy', codex: 'Codex', claude: 'Claude Code', deepseek: 'DeepSeek' };
  /** 把「本次读到了哪些来源、各多少条」拼成状态条后缀。
   *  找不到的来源也要说出来 —— 否则用户无法判断自己机器上那家客户端
   *  是被统计了 0 次，还是压根没被找到。 */
  function srcSummary(data) {
    var s = data && data.sources;
    if (!s) return '';
    var all = ['workbuddy', 'codex', 'claude', 'deepseek'];
    var got = [], missing = [];
    all.forEach(function (k) {
      if (s[k]) got.push(SRC_LABELS[k] + ' ' + fmtInt(s[k].events || 0));
      else missing.push(SRC_LABELS[k]);
    });
    var out = '';
    if (got.length) out += ' · ' + got.join(' / ');
    if (missing.length) out += ' · 未找到：' + missing.join('、');
    return out;
  }
  function boundsOf(mode, from, to) {
    if (mode === 'all') return [null, null];
    var end = new Date(); end.setHours(23, 59, 59, 999);
    if (mode === '7' || mode === '30') {
      var s = new Date(); s.setHours(0, 0, 0, 0);
      s.setDate(s.getDate() - (Number(mode) - 1));
      return [s.getTime(), end.getTime()];
    }
    if (mode === 'month') {
      var m = new Date(); m.setDate(1); m.setHours(0, 0, 0, 0);
      return [m.getTime(), end.getTime()];
    }
    var f = from ? new Date(from + 'T00:00:00').getTime() : null;
    var t = to ? new Date(to + 'T23:59:59.999').getTime() : null;
    return [f, t];
  }
  function currentEvents() {
    var r = rangeOf(CAT);
    var b = boundsOf(r.mode, r.from, r.to);
    if (b[0] === null && b[1] === null) return EVENTS;
    return EVENTS.filter(function (e) {
      return (b[0] === null || e.ts >= b[0]) && (b[1] === null || e.ts <= b[1]);
    });
  }
  function syncRangeButtons() {
    var r = rangeOf(CAT);
    var btns = document.querySelectorAll('[data-range]');
    for (var i = 0; i < btns.length; i++) {
      btns[i].classList.toggle('on', btns[i].getAttribute('data-range') === r.mode);
    }
  }

  /** 把当前分类的时间范围反映到日期字段上（切换分类时调用）。 */
  function syncDateInputs() {
    if (!EVENTS.length) return;
    var r = rangeOf(CAT);
    if (r.mode === 'custom') {
      setField('dateFrom', r.from || '');
      setField('dateTo', r.to || '');
      return;
    }
    if (r.mode === 'all') { initDateInputs(); return; }
    var b = boundsOf(r.mode, r.from, r.to);
    if (b[0]) setField('dateFrom', WBToken.localDate(b[0]));
    if (b[1]) setField('dateTo', WBToken.localDate(b[1]));
  }
  function initDateInputs() {
    if (!EVENTS.length) return;
    var min = EVENTS[0].ts, max = EVENTS[0].ts;
    for (var i = 1; i < EVENTS.length; i++) {
      if (EVENTS[i].ts < min) min = EVENTS[i].ts;
      if (EVENTS[i].ts > max) max = EVENTS[i].ts;
    }
    setField('dateFrom', WBToken.localDate(min));
    setField('dateTo', WBToken.localDate(max));
  }

  // ------------------------------------------------------------------ 日期滚轮
  // 桌面版 Safari/WebKit 的 <input type=date> 只弹「日历网格」，系统并不提供滚轮式
  // 日期控件。要 Apple 系统那种「三列滚轮 + 选中带」的手感，只能自绘：
  // 滚动吸附用 scroll-snap（系统原生惯性），选中判断用滚动位置推算索引。
  var DP_ROW = 33;                 // 行高（须与 index.html 的 --dp-row 一致）
  var DP = { el: null, side: 'from', cur: null, silent: false, anchor: null };

  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function isoOf(y, m, d) { return y + '-' + pad2(m) + '-' + pad2(d); }
  function daysInMonth(y, m) { return new Date(y, m, 0).getDate(); }
  function parseIso(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || '');
    if (!m) {
      var t = new Date();
      return { y: t.getFullYear(), m: t.getMonth() + 1, d: t.getDate() };
    }
    return { y: +m[1], m: +m[2], d: +m[3] };
  }

  /** 日期字段的读写：值存在 data-val 上，显示为 2026/05/28。 */
  function fieldId(side) { return side === 'from' ? 'dateFrom' : 'dateTo'; }
  function setField(id, iso) {
    var el = $(id);
    if (!el) return;
    el.setAttribute('data-val', iso || '');
    var v = el.querySelector ? el.querySelector('.dval') : null;
    if (v) v.textContent = iso ? String(iso).replace(/-/g, '/') : '年/月/日';
  }
  function getField(id) {
    var el = $(id);
    return el ? (el.getAttribute('data-val') || '') : '';
  }

  function ensurePicker() {
    if (DP.el) return DP.el;
    if (!document.body || !document.createElement) return null;   // 无 DOM 环境（测试桩）
    var dp = document.createElement('div');
    dp.className = 'dp';
    dp.setAttribute('role', 'dialog');
    dp.setAttribute('aria-label', '选择日期');
    dp.innerHTML =
      '<div class="dp-h"><span>年</span><span>月</span><span>日</span></div>' +
      '<div class="dp-box">' +
        '<div class="dp-band"></div>' +
        '<div class="dp-col" data-f="y" tabindex="-1"></div>' +
        '<div class="dp-col" data-f="m" tabindex="-1"></div>' +
        '<div class="dp-col" data-f="d" tabindex="-1"></div>' +
        '<div class="dp-mask t"></div><div class="dp-mask b"></div>' +
      '</div>';
    document.body.appendChild(dp);
    DP.el = dp;

    var cols = dp.querySelectorAll('.dp-col');
    for (var i = 0; i < cols.length; i++) {
      cols[i].addEventListener('scroll', function () { onDpScroll(this); }, { passive: true });
    }
    // 点外面 / Esc 关闭
    document.addEventListener('mousedown', function (e) {
      if (!DP.el || !DP.el.classList.contains('on')) return;
      var t = e.target;
      if (DP.el.contains(t)) return;
      if (t && t.closest && t.closest('.dfield')) return;   // 点另一个日期字段：直接切过去
      dpClose();
    }, true);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') dpClose();
    });
    // 页面滚动时跟着日期字段走（吸顶条本身不动，浮动层也要待在原位）
    if (window.addEventListener) {
      window.addEventListener('scroll', function () {
        if (DP.el && DP.el.classList.contains('on')) dpPlace();
      }, true);
    }
    return dp;
  }

  function dpCol(which) {
    return DP.el ? DP.el.querySelector('.dp-col[data-f="' + which + '"]') : null;
  }

  /** 重建某一列的选项；sel 为当前值，scroll=true 时直接把该值滚到中间。 */
  function dpFill(which, list, sel, scroll) {
    var col = dpCol(which);
    if (!col) return;
    var html = '', idx = 0;
    for (var i = 0; i < list.length; i++) {
      var o = list[i];
      if (o.v === sel) idx = i;
      html += '<div data-v="' + o.v + '">' + o.t + '</div>';
    }
    col.innerHTML = html;
    DP.silent = true;
    if (scroll) col.scrollTop = idx * DP_ROW;
    setTimeout(function () { DP.silent = false; }, 180);
    dpHighlight(col, idx);
  }

  function dpHighlight(col, idx) {
    var kids = col.children;
    if (!kids) return;
    for (var i = 0; i < kids.length; i++) {
      if (kids[i].classList) kids[i].classList.toggle('sel', i === idx);
    }
  }

  function dpYearRange(year) {
    var y0 = year, y1 = year;
    if (EVENTS.length) {
      for (var i = 0; i < EVENTS.length; i++) {
        var y = new Date(EVENTS[i].ts).getFullYear();
        if (y < y0) y0 = y;
        if (y > y1) y1 = y;
      }
    }
    return [y0 - 1, y1 + 1];       // 两端各留一年，避免只有一格
  }

  function dpBuild(cur) {
    var yr = dpYearRange(cur.y);
    var ys = [];
    for (var y = yr[0]; y <= yr[1]; y++) ys.push({ v: y, t: y + '年' });
    dpFill('y', ys, cur.y, true);

    var ms = [];
    for (var m = 1; m <= 12; m++) ms.push({ v: m, t: m + '月' });
    dpFill('m', ms, cur.m, true);

    dpFillDays(cur);
  }

  function dpFillDays(cur) {
    var n = daysInMonth(cur.y, cur.m), ds = [];
    for (var d = 1; d <= n; d++) ds.push({ v: d, t: d + '日' });
    dpFill('d', ds, Math.min(cur.d, n), true);
  }

  function onDpScroll(col) {
    if (!DP.el || DP.silent || !DP.cur) return;
    var f = col.getAttribute('data-f');
    var idx = Math.round(col.scrollTop / DP_ROW);
    var kids = col.children;
    if (!kids || !kids.length) return;
    if (idx < 0) idx = 0;
    if (idx > kids.length - 1) idx = kids.length - 1;
    var v = Number(kids[idx].getAttribute('data-v'));
    if (!v || DP.cur[f] === v) { dpHighlight(col, idx); return; }
    dpHighlight(col, idx);

    DP.cur[f] = v;
    if (f === 'y' || f === 'm') {
      var max = daysInMonth(DP.cur.y, DP.cur.m);
      if (DP.cur.d > max) { DP.cur.d = max; dpFillDays(DP.cur); dpEmit(); return; }
    }
    dpEmit();
  }

  var _dpEmitT = null;
  function dpEmit() {
    if (!DP.cur) return;
    var iso = isoOf(DP.cur.y, DP.cur.m, DP.cur.d);
    if (DP.side === 'from') {
      var to = getField('dateTo');
      if (to && iso > to) setField('dateTo', iso);      // 起点推过终点：把终点一起推
    } else {
      var f = getField('dateFrom');
      if (f && iso < f) setField('dateFrom', iso);
    }
    setField(fieldId(DP.side), iso);
    var r = rangeOf(CAT);
    r.mode = 'custom';
    r.from = getField('dateFrom') || null;
    r.to = getField('dateTo') || null;
    syncRangeButtons();
    clearTimeout(_dpEmitT);
    _dpEmitT = setTimeout(function () { if (EVENTS.length) renderAll(); }, 140);
  }

  function dpPlace() {
    var a = DP.anchor, dp = DP.el;
    if (!a || !a.getBoundingClientRect || !dp) return;
    var r = a.getBoundingClientRect();
    var w = dp.offsetWidth || 270, h = dp.offsetHeight || 210;
    var vw = window.innerWidth || 1200, vh = window.innerHeight || 800;
    var left = Math.min(r.left, vw - w - 10);
    if (left < 10) left = 10;
    var top = r.bottom + 8;
    if (top + h > vh - 10) top = Math.max(10, r.top - h - 8);
    dp.style.left = left + 'px';
    dp.style.top = top + 'px';
  }

  function dpOpen(side, anchor) {
    var dp = ensurePicker();
    if (!dp || !EVENTS.length) return;
    if (DP.el.classList.contains('on') && DP.side === side) { dpClose(); return; }
    DP.side = side;
    DP.anchor = anchor;
    var iso = getField(fieldId(side)) || WBToken.localDate(Date.now());
    DP.cur = parseIso(iso);
    dp.classList.add('on');                 // 先可见，scrollTop 才有效
    dpBuild(DP.cur);
    dpPlace();
    setFieldOpen(anchor, true);
    // display:none → block 的同一帧里滚不到目标位置，下一帧再对齐一次
    DP.silent = true;
    setTimeout(function () { dpRescroll(); DP.silent = false; }, 0);
    if (window.addEventListener) {
      window.removeEventListener('resize', dpPlace);
      window.addEventListener('resize', dpPlace);
    }
  }

  function dpIndex(col, v) {
    var kids = col.children;
    if (!kids) return -1;
    for (var i = 0; i < kids.length; i++) {
      if (Number(kids[i].getAttribute('data-v')) === v) return i;
    }
    return -1;
  }

  function dpRescroll() {
    ['y', 'm', 'd'].forEach(function (f) {
      var col = dpCol(f);
      if (!col || !DP.cur) return;
      var idx = dpIndex(col, DP.cur[f]);
      if (idx < 0) return;
      col.scrollTop = idx * DP_ROW;
      dpHighlight(col, idx);
    });
  }
  function setFieldOpen(anchor, on) {
    ['dateFrom', 'dateTo'].forEach(function (id) {
      var el = $(id);
      if (el && el.classList) el.classList.toggle('open', !!on && el === anchor);
    });
  }
  function dpClose() {
    if (!DP.el) return;
    DP.el.classList.remove('on');
    setFieldOpen(null, false);
    DP.anchor = null;
  }

  (function bindFilters() {
    var btns = document.querySelectorAll('[data-range]');
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener('click', function () {
        rangeOf(CAT).mode = this.getAttribute('data-range');
        dpClose();                       // 快捷区间会改字段，滚轮开着会显示旧值
        syncRangeButtons();
        syncDateInputs();
        renderAll();
      });
    }
    ['dateFrom', 'dateTo'].forEach(function (id) {
      $(id).addEventListener('click', function () {
        dpOpen(id === 'dateFrom' ? 'from' : 'to', this);
      });
    });
  })();

  // ------------------------------------------------------------------ 渲染入口
  function renderAll() {
    if (!EVENTS.length) return;
    var events = currentEvents();
    var agg = WBToken.aggregate(events);
    CUR = agg;
    COSTS = COST_OK ? window.WBPrice.summarize(events) : null;

    var r = rangeOf(CAT);
    $('rangeInfo').textContent = agg.range
      ? '当前区间：' + agg.range[0] + ' ~ ' + agg.range[1] + '（' + Object.keys(agg.byDay).length + ' 天）'
      : '当前区间无数据';

    renderCards(agg);
    renderQuota();
    renderTrend(agg);
    renderHeatmap(agg);
    renderHours();
    renderModels(agg);
    renderCosts(agg);
    renderSessions(agg);
    renderDays(agg);
    renderCover();
    renderCheck(agg);

    resultBox.classList.remove('hide');
    applyCat();
    reflowCharts();
  }

  // ------------------------------------------------------------------ KPI
  function renderCards(agg) {
    var t = agg.totals;
    var days = Object.keys(agg.byDay);
    var prompt = t.tin + t.cread;
    var cacheRatio = prompt ? t.cread / prompt : 0;

    var peakDay = null, peakModel = null;
    days.forEach(function (d) {
      if (!peakDay || agg.byDay[d].total > peakDay.total) peakDay = { d: d, total: agg.byDay[d].total };
    });
    Object.keys(agg.byDayModel).forEach(function (k) {
      var v = agg.byDayModel[k];
      if (!peakModel || v > peakModel.total) {
        var parts = k.split('|');
        peakModel = { d: parts[0], agent: parts[1], total: v };
      }
    });

    var cards = [
      ['Token 总用量', fmtCompact(t.total), '精确 ' + fmtInt(t.total)],
      ['调用次数', fmtInt(t.events), '日均 ' + fmtInt(t.events / Math.max(days.length, 1))],
      // 7 列后卡片变窄，副行只留 MM-DD，完整年月日放到 title 里
      ['活跃天数', String(days.length),
        agg.range ? agg.range[0].slice(5) + ' ~ ' + agg.range[1].slice(5) : '-'],
      ['缓存读取占比', fmtPct(cacheRatio), fmtCompact(t.cread) + ' Token'],
      ['调用模型数量', String(Object.keys(agg.byModel).length), '会话数 ' + fmtInt(Object.keys(agg.bySession).length)],
      ['单日 Token 峰值', peakDay ? fmtCompact(peakDay.total) : '-', peakDay ? peakDay.d : '-'],
      ['单日模型峰值', peakModel ? shortModel(peakModel.agent) : '-',
        peakModel ? peakModel.d + ' · ' + fmtCompact(peakModel.total) : '-']
    ];
    $('cards').innerHTML = cards.map(function (c, i) {
      var cls = 'v' + (i === 6 || String(c[1]).length > 8 ? ' sm' : '');
      return '<div class="card"><div class="k">' + esc(c[0]) + '</div>' +
             '<div class="' + cls + '" title="' + esc(c[1]) + '">' + esc(c[1]) + '</div>' +
             '<div class="s" title="' + esc(c[2]) + '">' + esc(c[2]) + '</div></div>';
    }).join('');
  }

  // ------------------------------------------------------------------ 趋势图
  var TREND = [];   // 悬停数据

  function renderTrend(agg) {
    var days = Object.keys(agg.byDay).sort();
    if (!days.length) {
      $('yaxisBox').innerHTML = '';
      $('chartview').innerHTML = '<p class="empty">区间内没有数据</p>';
      return;
    }
    var rows = days.map(function (d) {
      var b = agg.byDay[d];
      return { date: d, tin: b.tin, tout: b.tout, cread: b.cread, total: b.total };
    });

    var H = 300, PT = 18, IH = 250;
    var n = rows.length;
    var ymax = niceCeil(Math.max.apply(null, rows.map(function (r) { return r.total; })) || 1);
    var W = Math.max(n * DAY_W + 14, 300);

    function x(i) { return DAY_W * i + DAY_W / 2; }
    function y(v) { return PT + IH - IH * v / ymax; }

    // 固定 y 轴
    var ax = ['<svg class="yaxis" width="62" height="300" viewBox="0 0 62 300" aria-hidden="true">'];
    [0, 0.25, 0.5, 0.75, 1].forEach(function (f) {
      var gy = PT + IH - IH * f;
      ax.push('<text x="54" y="' + (gy + 4).toFixed(1) + '" class="axis" text-anchor="end">' +
              fmtCompact(ymax * f) + '</text>');
    });
    ax.push('</svg>');
    $('yaxisBox').innerHTML = ax.join('');

    var series = [['tin', 'in'], ['tout', 'out'], ['cread', 'cache']];
    var p = ['<svg id="trend" width="' + W + '" height="300" viewBox="0 0 ' + W + ' 300" class="chartwide" role="img" aria-label="每日 token 消耗趋势">'];
    [0, 0.25, 0.5, 0.75, 1].forEach(function (f) {
      var gy = PT + IH - IH * f;
      p.push('<line class="grid" x1="0" y1="' + gy.toFixed(1) + '" x2="' + W + '" y2="' + gy.toFixed(1) + '"/>');
    });

    var cum = rows.map(function () { return 0; });
    series.forEach(function (s) {
      var top = [], bot = [];
      rows.forEach(function (r, i) {
        var v0 = cum[i], v1 = v0 + r[s[0]];
        cum[i] = v1;
        top.push([x(i), y(v1)]);
        bot.push([x(i), y(v0)]);
      });
      var poly = top.map(function (pt) { return pt[0].toFixed(1) + ',' + pt[1].toFixed(1); }).join(' ');
      var back = bot.slice().reverse().map(function (pt) { return pt[0].toFixed(1) + ',' + pt[1].toFixed(1); }).join(' ');
      p.push('<polygon points="' + poly + ' ' + back + '" class="ser-' + s[1] + '" fill-opacity="0.26"/>');
      p.push('<polyline points="' + poly + '" fill="none" class="ln-' + s[1] + '" stroke-width="1.8"/>');
    });

    TREND = [];
    rows.forEach(function (r, i) {
      var px = x(i), py = y(r.total);
      p.push('<circle cx="' + px.toFixed(1) + '" cy="' + py.toFixed(1) + '" r="3" class="dotpt"/>');
      TREND.push({ d: r.date, px: px, py: py, total: r.total, tin: r.tin, tout: r.tout, cread: r.cread });
      p.push('<text x="' + px.toFixed(1) + '" y="' + (H - 10) + '" class="axis" text-anchor="middle">' + r.date.slice(5) + '</text>');
    });
    rows.forEach(function (r, i) {
      p.push('<rect class="hov" data-i="' + i + '" x="' + (DAY_W * i) + '" y="' + PT +
             '" width="' + DAY_W + '" height="' + IH + '" fill="transparent"/>');
    });
    p.push('<line id="guide" x1="0" y1="' + PT + '" x2="0" y2="' + (PT + IH) +
           '" class="guideline" stroke-dasharray="3,3" stroke-width="1" opacity="0"/>');
    p.push('<circle id="hovdot" r="4.5" class="hovdot" stroke-width="1.5" opacity="0"/>');
    p.push('</svg>');
    $('chartview').innerHTML = p.join('');

    var sc = $('scrollx');
    sc.scrollLeft = sc.scrollWidth;
    bindTrendHover();
  }

  function bindTrendHover() {
    var svg = $('trend'), tip = $('tip'), guide = $('guide'), dot = $('hovdot');
    if (!svg) return;
    var view = $('scrollx');   // 定位基准用可视滚动区，不是会滚动的内层容器
    function show(i) {
      var d = TREND[i];
      if (!d) return;
      guide.setAttribute('x1', d.px); guide.setAttribute('x2', d.px);
      guide.setAttribute('opacity', '1');
      dot.setAttribute('cx', d.px); dot.setAttribute('cy', d.py);
      dot.setAttribute('opacity', '1');
      tip.innerHTML =
        '<div class="tip-d">' + d.d + '</div>' +
        '<div class="tip-r"><i class="dot-total"></i>Token 消耗总量：<b>' + fmtCompact(d.total) + '</b></div>' +
        '<div class="tip-r"><i class="dot-in"></i>输入：<b>' + fmtCompact(d.tin) + '</b></div>' +
        '<div class="tip-r"><i class="dot-out"></i>输出：<b>' + fmtCompact(d.tout) + '</b></div>' +
        '<div class="tip-r"><i class="dot-cache"></i>缓存读取：<b>' + fmtCompact(d.cread) + '</b></div>';
      tip.style.opacity = '1';
      var pr = svg.getBoundingClientRect();
      var vr = view.getBoundingClientRect();
      var sx = pr.left + d.px - vr.left;
      var tw = tip.offsetWidth;
      var left = Math.min(Math.max(sx - tw / 2, 4), Math.max(vr.width - tw - 4, 4));
      tip.style.left = left + 'px';
    }
    function hide() {
      tip.style.opacity = '0';
      guide.setAttribute('opacity', '0');
      dot.setAttribute('opacity', '0');
    }
    var rects = svg.querySelectorAll('.hov');
    for (var i = 0; i < rects.length; i++) {
      rects[i].addEventListener('mouseenter', function () { show(+this.getAttribute('data-i')); });
      rects[i].addEventListener('mouseleave', hide);
    }
  }

  // ------------------------------------------------------------------ 热力图
  function renderHeatmap(agg) {
    var days = Object.keys(agg.byDay).sort();
    var agents = Object.keys(agg.byModel)
      .sort(function (a, b) { return agg.byModel[b].total - agg.byModel[a].total; })
      .slice(0, HEAT_TOP);
    if (!days.length || !agents.length) {
      $('heatmapBox').innerHTML = '<p class="empty">区间内没有数据</p>';
      return;
    }
    var vals = [];
    agents.forEach(function (a) {
      days.forEach(function (d) {
        var v = agg.byDayModel[d + '|' + a];
        if (v > 0) vals.push(v);
      });
    });
    var vmin = vals.length ? Math.min.apply(null, vals) : 1;
    var vmax = vals.length ? Math.max.apply(null, vals) : 1;
    var lo = Math.log10(Math.max(vmin, 1));
    var hi = Math.log10(Math.max(vmax, 1));
    // 色阶随主题切换：浅色是「淡蓝 → 深蓝」，深色是「深灰 → 亮蓝」
    var dark = isDarkMode();
    var PAL = dark ? [[36, 48, 72], [110, 160, 255]] : [[219, 234, 254], [30, 58, 138]];
    function color(v) {
      if (!v) return 'var(--hm-empty)';
      var t = hi > lo ? (Math.log10(v) - lo) / (hi - lo) : 1;
      return lerpColor(PAL[0], PAL[1], t);
    }

    var head = days.map(function (d) {
      return '<th>' + d.slice(5) + '</th>';
    }).join('');
    var rows = agents.map(function (a) {
      var name = shortModel(a);
      var cells = days.map(function (d) {
        var v = agg.byDayModel[d + '|' + a] || 0;
        return '<td class="cell"><i style="background:' + color(v) + '" title="' +
               d + ' ' + esc(name) + '：' + fmtInt(v) + ' token"></i></td>';
      }).join('');
      var tot = agg.byModel[a].total;
      // 模型名 + 来源徽标；列宽由 colgroup 内联给出（与 fitHeatmap 的常量同源）
      return '<tr><th class="hmlabel" title="' + esc(name + ' · ' + a) + '">' +
             '<span class="hmname">' + esc(name) + '</span>' + sourceBadge(a) + '</th>' +
             cells + '<td class="hmtotal">' + fmtCompact(tot) + '</td></tr>';
    }).join('');

    var colgroup = '<colgroup><col class="c-label" style="width:' + HEAT_LABEL_W + 'px">';
    for (var ci = 0; ci < days.length; ci++) colgroup += '<col class="c-day">';
    colgroup += '<col class="c-total" style="width:' + HEAT_TOTAL_W + 'px"></colgroup>';

    // 表头与表体分属两个滚动上下文：表体管纵向（表头不动），横向由表体驱动、表头跟随。
    $('heatmapBox').innerHTML =
      '<div class="hmbox">' +
        '<div class="hm-h"><table class="hm">' + colgroup +
          '<thead><tr><th class="hmlabel">模型</th>' + head +
          '<th class="hmtotal">合计</th></tr></thead>' +
        '</table></div>' +
        '<div class="hm-b"><table class="hm">' + colgroup +
          '<tbody>' + rows + '</tbody>' +
        '</table></div>' +
      '</div>';

    fitHeatmap();
  }

  /**
   * 热力图布局：
   *  ① 色块列宽现算，可视区恰好容纳整数列 —— 否则滚动停半列，右边缘永远露半个色块；
   *  ② 纵向高度取整行，不切半行；超出的部分靠滚动；
   *  ③ 表头容器扣掉表体纵向滚动条宽度，两张表的可视宽度才严格相等。
   */
  function fitHeatmap() {
    if (!document.querySelector) return;
    var box = document.querySelector('#heatmapBox .hmbox');
    if (!box || !box.querySelector) return;
    var body = box.querySelector('.hm-b');
    var headBox = box.querySelector('.hm-h');
    if (!body || !headBox || !box.querySelectorAll) return;
    var tabs = box.querySelectorAll('table.hm');
    if (!tabs || tabs.length < 2) return;

    var cols = tabs[1].querySelectorAll('col.c-day');
    if (!cols || !cols.length) return;

    // 先把纵向限高定下来，再量宽度 —— 顺序不能反：
    // 限高会让表体长出纵向滚动条（经典滚动条模式下占 15px），
    // 若先量宽度再限高，算出的列宽会偏大、表头也比表体宽，滑到最右时表头/合计列会错位。
    var trs = body.querySelectorAll('tbody tr');
    if (trs && trs.length) {
      var rh = trs[0].offsetHeight || 0;
      if (rh > 0) {
        var show = Math.min(trs.length, HEAT_ROWS);
        body.style.maxHeight = (show * rh) + 'px';
      }
    }

    var cw = body.clientWidth || 0;
    if (cw <= 0) return;                       // 分类隐藏时宽度为 0，切回来会再算
    var avail = cw - HEAT_LABEL_W - HEAT_TOTAL_W;
    if (avail <= 0) return;

    var n = Math.max(3, Math.floor(avail / HEAT_MIN_W));
    var w = avail / n;                         // 不取整：n 列正好铺满
    for (var i = 0; i < cols.length; i++) cols[i].style.width = w + 'px';

    // 表宽取整到列宽整数倍 + 两端固定列 —— 这样最大滚动量 = (N-n)*w，正好停在一列边界
    var sbw = (body.offsetWidth || 0) - cw;    // 纵向滚动条占掉的宽度（overlay 模式下为 0）
    headBox.style.marginRight = (sbw > 0 ? sbw : 0) + 'px';
    var full = HEAT_LABEL_W + cols.length * w + HEAT_TOTAL_W;
    for (var t = 0; t < tabs.length; t++) tabs[t].style.width = full + 'px';

    body._cellW = w;
    body._labelW = HEAT_LABEL_W;

    body.scrollLeft = body.scrollWidth;
    if (headBox && Math.abs(headBox.scrollLeft - body.scrollLeft) > 0.5) {
      headBox.scrollLeft = body.scrollLeft;
    }
    bindHmSync();
  }

  /**
   * 横向：表头双向同步 + 停稳后对齐整列。
   *
   * 三处刻意的写法（都为了"顺手"，别改回去）：
   *  ① 同步只在差值 > 0.5px 时写，避免同一帧重复写 scrollLeft 触发无谓重绘；
   *  ② 吸附用**瞬间对齐**（behavior:auto），**不用 smooth** —— smooth 会在
   *     用户手势刚结束时再放一段动画，手指再动就中断，观感就是"滑完自己又挪一下"；
   *  ③ 只有偏移 > 2px 才修，避免已经对齐时被反复微调、滚动手感发黏。
   */
  function bindHmSync() {
    if (!document.querySelector) return;
    var body = document.querySelector('#heatmapBox .hm-b');
    if (!body || body._snapBound) return;
    body._snapBound = true;
    var headBox = document.querySelector('#heatmapBox .hm-h');

    body.addEventListener('scroll', function () {
      var el = this;
      if (headBox && Math.abs(headBox.scrollLeft - el.scrollLeft) > 0.5) {
        headBox.scrollLeft = el.scrollLeft;           // 表头跟随（同帧直写，不加防抖）
      }
      clearTimeout(el._snapT);
      el._snapT = setTimeout(function () {
        var w = el._cellW || 56;
        var max = (el.scrollWidth || 0) - (el.clientWidth || 0);
        var x = Math.round(el.scrollLeft / w) * w;
        if (x > max) x = max;
        if (x < 0) x = 0;
        if (Math.abs(x - el.scrollLeft) > 2 && el.scrollTo) {
          el.scrollTo({ left: x, behavior: 'auto' });
          if (headBox) headBox.scrollLeft = x;
        }
      }, 110);
    }, { passive: true });

    // 反向：直接拖表头时把表体带上（值相同就不再触发事件，不会来回抖）
    if (headBox && headBox.addEventListener) {
      headBox.addEventListener('scroll', function () {
        if (Math.abs(body.scrollLeft - this.scrollLeft) > 0.5) body.scrollLeft = this.scrollLeft;
      }, { passive: true });
    }
  }

  // ------------------------------------------------------------------ 半小时分布
  /** 一天内按 30 分钟切 48 个区间。
   *  结构上把「绘图区」与「坐标轴」拆成两行：柱子全部贴同一条基线，
   *  数字统一放在轴下方（数学教科书里的条形统计图那种），不再混在柱子里。 */
  function renderHours() {
    var BUCKETS = 48;
    var PLOT_H = 128;
    // 「今日使用情况」：只统计**今天**的事件。此前是全区间按钟点混桶，
    // 改名后口径必须跟着改，否则"今日"是假的（选近 30 天会把 30 天叠在一天上）。
    var today = WBToken.localDate(Date.now());
    var evs = currentEvents().filter(function (e) {
      return WBToken.localDate(e.ts) === today;
    });
    var sums = new Array(BUCKETS);
    var cnts = new Array(BUCKETS);
    var aggBuckets = new Array(BUCKETS);
    for (var i = 0; i < BUCKETS; i++) {
      sums[i] = 0; cnts[i] = 0;
      aggBuckets[i] = { tin: 0, tout: 0, cread: 0 };
    }

    for (var j = 0; j < evs.length; j++) {
      var e = evs[j];
      var dt = new Date(e.ts);
      var b = Math.floor((dt.getHours() * 60 + dt.getMinutes()) / 30);
      if (b < 0) b = 0;
      if (b >= BUCKETS) b = BUCKETS - 1;
      sums[b] += e.tin + e.cread + e.tout + e.cwrite + (e.reasoning || 0);
      aggBuckets[b].tin += e.tin;
      aggBuckets[b].tout += e.tout;
      aggBuckets[b].cread += e.cread;
      cnts[b] += 1;
    }

    var max = 1;
    for (var k = 0; k < BUCKETS; k++) if (sums[k] > max) max = sums[k];

    function hhmm(m) {
      var h = Math.floor(m / 60), mm = m % 60;
      return (h < 10 ? '0' : '') + h + ':' + (mm < 10 ? '0' : '') + mm;
    }

    var bars = [], axis = [];
    HOUR_DATA = [];
    for (var t = 0; t < BUCKETS; t++) {
      var v = sums[t];
      var b2 = aggBuckets[t];
      var hpx = v > 0 ? Math.max(2, Math.round(v / max * PLOT_H)) : 1;
      var start = t * 30;
      HOUR_DATA.push({
        label: hhmm(start) + '–' + hhmm(start + 29),
        total: v, tin: b2.tin, tout: b2.tout, cread: b2.cread, cnt: cnts[t]
      });
      bars.push('<div class="hb' + (v > 0 ? '' : ' zero') + '" data-i="' + t + '">' +
                '<i style="height:' + hpx + 'px"></i></div>');
      // 轴刻度：每 2 小时标一次（每 4 根柱），标在整点起点那一格
      axis.push('<span>' + (t % 4 === 0 ? hhmm(start) : '') + '</span>');
    }
    $('hoursBox').innerHTML = bars.join('');
    $('hoursAxis').innerHTML = axis.join('');
    bindHourHover();
  }

  /** 悬停提示：与趋势图同款卡片（标题 + 带色圆点明细），跟随柱子定位。 */
  function bindHourHover() {
    var box = $('hoursBox');
    var tip = $('htip');
    if (!box || !tip || !box.querySelectorAll) return;
    var nodes = box.querySelectorAll('.hb');
    if (!nodes || !nodes.length) return;
    var wrap = $('hoursWrap') || box.parentNode;

    var mx = null, my = null;      // 最近一次鼠标位置（视口坐标）
    var cur = -1;                  // 当前悬停的柱子下标

    function place() {
      if (cur < 0) return;
      var node = nodes[cur], d = HOUR_DATA[cur];
      if (!node || !d) return;
      var br = node.getBoundingClientRect();
      var wr = wrap.getBoundingClientRect();
      var tw = tip.offsetWidth || 210, th = tip.offsetHeight || 150;
      // 横向：以光标为中心，夹在容器内
      var cx = (mx === null ? br.left + br.width / 2 : mx);
      var left = Math.min(Math.max(cx - wr.left - tw / 2, 4), Math.max(wr.width - tw - 4, 4));
      // 纵向：整张卡片放到绘图区上方 —— 一张卡片比 30 分钟的柱子高得多，
      // 只要还压在图表范围内就必然遮住相邻数据条，所以直接抬到图表之上。
      var top = -th - 10;
      // 图表贴着视口顶部时上方没有空间，改放到坐标轴下方（同样不压数据条）
      if (wr.top + top < 8) top = wr.height + 6;
      tip.style.left = left + 'px';
      tip.style.top = top + 'px';
    }

    function show(i) {
      var d = HOUR_DATA[i];
      if (!d) return;
      cur = i;
      tip.innerHTML =
        '<div class="tip-d">' + d.label + '</div>' +
        '<div class="tip-r"><i class="dot-total"></i>Token 消耗总量：<b>' + fmtCompact(d.total) + '</b></div>' +
        '<div class="tip-r"><i class="dot-in"></i>输入：<b>' + fmtCompact(d.tin) + '</b></div>' +
        '<div class="tip-r"><i class="dot-out"></i>输出：<b>' + fmtCompact(d.tout) + '</b></div>' +
        '<div class="tip-r"><i class="dot-cache"></i>缓存读取：<b>' + fmtCompact(d.cread) + '</b></div>' +
        '<div class="tip-r" style="border-top:1px solid var(--line2);margin-top:5px;padding-top:5px">' +
          '<i class="dot-cnt"></i>调用次数：<b>' + fmtInt(d.cnt) + '</b></div>';
      tip.style.opacity = '1';
      place();
    }
    function hide() { cur = -1; tip.style.opacity = '0'; }

    // 记录光标位置：提示框跟着光标走，而不是固定在柱子中心
    box.addEventListener('mousemove', function (ev) {
      mx = ev.clientX; my = ev.clientY;
      if (cur >= 0) place();
    });

    for (var i = 0; i < nodes.length; i++) {
      (function (k) {
        nodes[k].addEventListener('mouseenter', function () { show(k); });
        nodes[k].addEventListener('mouseleave', hide);
      })(i);
    }
    wrap.addEventListener('mouseleave', function () { mx = null; my = null; hide(); });
  }

  // ------------------------------------------------------------------ 模型排行 + 成本
  function renderModels(agg) {
    var agents = Object.keys(agg.byModel).sort(function (a, b) {
      return agg.byModel[b].total - agg.byModel[a].total;
    });
    var grand = agg.totals.total || 1;
    var top = agents.length ? agg.byModel[agents[0]].total : 1;

    var rows = agents.map(function (a, i) {
      var b = agg.byModel[a];
      var name = shortModel(a);
      var prompt = b.tin + b.cread;
      var hit = prompt ? b.cread / prompt : 0;
      var w = top ? (b.total / top * 100) : 0;

      var c = COSTS ? COSTS.byAgent[a] : null;
      var costCell;
      if (!c) costCell = '<span class="muted">—</span>';
      else if (c.status === 'priced') costCell = fmtMoney(c.amount, c.currency);
      else if (c.status === 'free') costCell = '<span class="muted">免费</span>';
      else costCell = '<span class="muted" title="' + esc((COSTS.unpriced[a] || {}).why || '价格表未收录') + '">未定价</span>';

      return '<tr><td>' + (i + 1) + '</td>' +
        '<td title="' + esc(name + ' · ' + a) + '">' + esc(name) + sourceBadge(a) + retiredBadge(a) + '</td>' +
        '<td class="num">' + fmtInt(b.events) + '</td>' +
        '<td class="num">' + fmtCompact(b.tin) + '</td>' +
        '<td class="num">' + fmtCompact(b.tout) + '</td>' +
        '<td class="num">' + fmtCompact(b.cread) + '</td>' +
        '<td class="num">' + fmtPct(hit) + '</td>' +
        '<td class="num"><b>' + fmtCompact(b.total) + '</b></td>' +
        '<td class="num">' +
          '<span class="meter" style="display:inline-block;width:34px;height:7px;border-radius:2px;margin-right:5px;vertical-align:1px">' +
          '<span style="display:block;width:' + w.toFixed(1) + '%;height:7px;border-radius:2px;background:var(--blue)"></span></span>' +
          fmtPct(b.total / grand) + '</td>' +
        '<td class="num">' + costCell + '</td></tr>';
    });
    $('modelBody').innerHTML = rows.length ? rows.join('') : '<tr><td colspan="10" class="muted">无数据</td></tr>';
  }

  // ------------------------------------------------------------------ 成本估算区
  /** 表头与表体分表：表头固定在滚动容器外，只让表体滚动。
   *  两张表共用同一份 colgroup 与 table-layout:fixed，列宽天然对齐；
   *  避开了「sticky 表头 + max-height 容器」在 WebKit 下的错位问题。 */
  function tblSplit(cols, headCells, bodyRows, maxH, cls) {
    var c = cls ? ' class="' + cls + '"' : '';
    // maxH<=0 时不写限高样式（否则 max-height:0 会把内容全部藏掉）
    var bStyle = maxH > 0 ? ' style="max-height:' + maxH + 'px"' : '';
    return '<div class="tbl">' +
      '<div class="tbl-h"><table' + c + '><colgroup>' + cols + '</colgroup>' +
        '<thead><tr>' + headCells + '</tr></thead></table></div>' +
      '<div class="tbl-b"' + bStyle + '>' +
        '<table' + c + '><colgroup>' + cols + '</colgroup><tbody>' + bodyRows + '</tbody></table></div>' +
      '</div>';
  }

  function renderCosts(agg) {
    var box = $('costBox');
    var cardBox = $('costCards');
    if (!COST_OK || !COSTS) {
      var msg = '<p class="muted" style="font-size:12.5px;margin:0">' +
        '成本功能未启用，其余统计不受影响。</p>';
      box.innerHTML = msg;
      if (cardBox) cardBox.innerHTML = '<div class="muted" style="font-size:12.5px">成本功能未启用</div>';
      return;
    }

    var html = '';
    // 口径说明：成本估算的定义就是「每个模型按其 API 付费价整合计算」。
    // 公式属于实现细节，用户需要知道的是「什么口径、能不能横向比、未定价怎么处理」。
    html += '<p class="muted" style="font-size:12px;margin:0 0 12px">' +
      '把每个模型的用量按厂商官网的 <b>API 单价</b> 逐次折算后合并，' +
      '所以不同客户端、订阅套餐或限免渠道的用量都按同一把尺子计价，可以横向比较。' +
      '这是<b>等价价值</b>，不等于实际支出。匹配不到单价的模型标为「未定价」，' +
      '不会按 0 元混入合计。</p>';

    // 合计（按币种分列；跨币种合并需要汇率）
    // 标题必须带币种与口径：只说「区间合计」会让人不知道两个数字分别是什么币种。
    var cur = COSTS.byCurrency || {};
    var keys = Object.keys(cur).sort();
    var cards = keys.map(function (c) {
      return '<div class="card"><div class="k">API 等价成本（' + c + '）</div>' +
             '<div class="v">' + fmtMoney(cur[c], c) + '</div>' +
             '<div class="s">按官方 API 单价逐次折算</div></div>';
    });
    // 「折算合计」卡已按需求移除：跨币种合并会引入汇率假设，且与其余三张不同量纲。
    // 需要合并总额时，在 prices.js 填 fx 后自行计算即可。
    var totalEvents = agg.totals.events || 0;
    if (COSTS.peakEvents || COSTS.offpeakEvents) {
      var peakShare = totalEvents ? COSTS.peakEvents / totalEvents : 0;
      cards.push('<div class="card"><div class="k">峰谷分布（高峰 / 总调用）</div>' +
                 '<div class="v" style="font-size:17px">' + fmtInt(COSTS.peakEvents) + ' / ' +
                 fmtInt(totalEvents) + '</div>' +
                 '<div class="s" title="仅统计有峰谷价的模型（如 DeepSeek）；' +
                 '固定价模型的调用不计入高峰">高峰占比 ' + fmtPct(peakShare) +
                 ' · 空闲 ' + fmtInt(COSTS.offpeakEvents) + ' 次（仅峰谷计价模型）</div></div>');
    }
    // 合计卡放到「概览」里（成本区只留明细），这里不拼进 costBox
    if (cardBox) {
      cardBox.innerHTML = cards.length ? cards.join('')
        : '<div class="muted" style="font-size:12.5px">当前区间没有可定价的调用</div>';
    }

    // 按模型的成本明细
    var rows = Object.keys(COSTS.byAgent)
      .filter(function (a) { return COSTS.byAgent[a].status === 'priced'; })
      .sort(function (a, b) { return COSTS.byAgent[b].amount - COSTS.byAgent[a].amount; })
      .map(function (a) {
        var s = COSTS.byAgent[a];
        var share = cur[s.currency] ? s.amount / cur[s.currency] : 0;
        return '<tr><td title="' + esc(shortModel(a) + ' · ' + a) + '">' +
               esc(shortModel(a)) + sourceBadge(a) + retiredBadge(a) + '</td>' +
               '<td class="num">' + fmtCompact(s.tokens) + '</td>' +
               '<td class="num"><b>' + fmtMoney(s.amount, s.currency) + '</b></td>' +
               '<td class="num">' + fmtPct(share) + '</td></tr>';
      });
    if (rows.length) {
      // 「计价条目」列显示的是内部单价键名（如 glm-5.3），对用户无意义，已移除
      var ccols = '<col style="width:46%"><col style="width:14%">' +
                  '<col style="width:22%"><col style="width:18%">';
      var chead = '<th>模型</th><th class="num">Token</th>' +
                  '<th class="num">API 等价价值</th><th class="num">占该币种</th>';
      html += '<div style="margin-bottom:10px">' + tblSplit(ccols, chead, rows.join(''), 260) + '</div>';
    }

    // 未定价清单：分三类说明，避免「为什么没价」无从查证
    var un = Object.keys(COSTS.unpriced);
    if (un.length) {
      var T0 = window.WB_PRICES || {};
      var below = {};
      ((T0.policy || {}).below_threshold || []).forEach(function (k) { below[k.toLowerCase()] = 1; });
      var groups = { below: [], other: [] };
      un.forEach(function (ag) {
        var nm = sourceOf(ag).name.toLowerCase();
        (below[nm] ? groups.below : groups.other).push(ag);
      });
      var allTok = 0;
      un.forEach(function (ag) { allTok += COSTS.unpriced[ag].tokens; });

      html += '<div style="border-top:1px solid var(--line);padding-top:10px;margin-top:4px;overflow-x:auto">' +
        '<div style="font-size:12.5px;font-weight:600;margin-bottom:4px">未定价模型 ' + un.length +
        ' 个<span class="muted" style="font-weight:400">（未计入上方合计，否则合计会偏低）</span></div>' +
        '<p class="muted" style="font-size:11.5px;margin:0 0 8px;line-height:1.7">' +
        '路由别名（如 auto）与内部代号无法对应到具体型号，暂不单独定价；' +
        '调用量极低的模型也不单列。</p>';

      function unpricedTable(list, title) {
        if (!list.length) return '';
        var body = list.sort(function (x, y) {
          return COSTS.unpriced[y].tokens - COSTS.unpriced[x].tokens;
        }).map(function (ag) {
          var nm = sourceOf(ag).name;
          return '<tr><td title="' + esc(nm + ' · ' + ag) + '">' + esc(nm) +
                 sourceBadge(ag) + '</td>' +
                 '<td class="num">' + fmtCompact(COSTS.unpriced[ag].tokens) + '</td>' +
                 '<td class="muted" style="white-space:normal">' +
                 esc(COSTS.unpriced[ag].why || '价格表未收录') + '</td></tr>';
        }).join('');
        return '<div style="font-size:11.5px;color:var(--sub);margin:8px 0 4px">' +
               title + '（' + list.length + ' 个）</div>' +
               '<table><colgroup><col style="width:34%"><col style="width:14%"><col style="width:52%"></colgroup>' +
               '<thead><tr><th>模型</th><th>Token</th><th>原因</th></tr></thead>' +
               '<tbody>' + body + '</tbody></table>';
      }

      html += unpricedTable(groups.other, '无法定价 / 未查到公开价');
      html += unpricedTable(groups.below, '调用量占比很低，未单独收录');
      html += '</div>';
    }

    // 单价表不呈现给用户：它是应用内部的计价依据，用户只需要"算出来的数是可靠的"。
    // 需要核对单价时跑 check-prices.py（会打印每条单价、来源与复核到期情况）。
    box.innerHTML = html;
  }

  // ------------------------------------------------------------------ 会话
  /**
   * 今日会话：只看**今天**，按会话聚合调用次数与 Token。
   * 与「会话排行」（整区间）分开，回答"今天在哪些会话里花掉了用量"。
   * 用 EVENTS（全量）而非 currentEvents()：今日表不该被上面的时间筛选影响，
   * 否则选"上个月"时它会空掉，与标题不符。
   */
  function renderTodaySessions() {
    var box = $('todayBody');
    if (!box) return;
    var today = WBToken.localDate(Date.now());
    var byKey = {};
    for (var i = 0; i < EVENTS.length; i++) {
      var e = EVENTS[i];
      if (WBToken.localDate(e.ts) !== today) continue;
      var k = WBToken.sessionKey(e);
      var s = byKey[k] || (byKey[k] = { events: 0, total: 0, models: {} });
      s.events += 1;
      s.total += e.tin + e.cread + e.tout + e.cwrite + (e.reasoning || 0);
      s.models[e.agent] = (s.models[e.agent] || 0) + (e.tin + e.cread + e.tout);
    }
    var keys = Object.keys(byKey).sort(function (a, b) {
      return byKey[b].total - byKey[a].total;
    });
    var grand = 0;
    keys.forEach(function (k) { grand += byKey[k].total; });
    if (grand <= 0) grand = 1;
    box.innerHTML = keys.slice(0, 100).map(function (k) {
      var s = byKey[k];
      var top = Object.keys(s.models).sort(function (x, y) {
        return s.models[y] - s.models[x];
      })[0];
      var label = sessionLabel(k);
      return '<tr><td class="sessname" title="' + esc(label + (k ? ' · ' + k : '')) + '">' +
             '<span class="nm">' + esc(label) + '</span></td>' +
             '<td title="' + esc(top ? shortModel(top) : '') + '">' +
             esc(top ? shortModel(top) : '-') + '</td>' +
             '<td class="num">' + fmtInt(s.events) + '</td>' +
             '<td class="num"><b>' + fmtCompact(s.total) + '</b></td>' +
             '<td class="num">' + fmtPct(s.total / grand) + '</td></tr>';
    }).join('') || '<tr><td colspan="5" class="muted">今天还没有调用记录</td></tr>';
  }

  function renderSessions(agg) {
    renderTodaySessions();
    var meta = {};
    currentEvents().forEach(function (e) {
      var k = WBToken.sessionKey(e);
      if (!meta[k]) meta[k] = { first: e.ts, last: e.ts, models: {} };
      if (e.ts < meta[k].first) meta[k].first = e.ts;
      if (e.ts > meta[k].last) meta[k].last = e.ts;
      meta[k].models[e.agent] = (meta[k].models[e.agent] || 0) + (e.tin + e.cread + e.tout);
    });

    var keys = Object.keys(agg.bySession).sort(function (a, b) {
      return agg.bySession[b].total - agg.bySession[a].total;
    });

    $('sessBody').innerHTML = keys.slice(0, 200).map(function (k) {
      var b = agg.bySession[k], m = meta[k] || { first: 0, last: 0, models: {} };
      var topModel = Object.keys(m.models).sort(function (x, y) { return m.models[y] - m.models[x]; })[0];
      var label = sessionLabel(k);
      var uid = k === WBToken.UNKNOWN_SESSION ? '' : k.slice(0, 8);
      var d1 = m.first ? WBToken.localDate(m.first) : '-';
      var d2 = m.last ? WBToken.localDate(m.last) : '-';
      return '<tr style="cursor:pointer" data-sess="' + esc(k) + '">' +
        '<td class="sessname" title="' + esc(label + (uid ? ' · ' + k : '')) + '">' +
          '<span class="nm">' + esc(label) + '</span>' +
          (uid ? '<span class="uid">' + esc(uid) + '</span>' : '') +
        '</td>' +
        '<td class="muted num">' + (d1 === d2 ? d1 : d1 + ' ~ ' + d2) + '</td>' +
        '<td title="' + esc(topModel ? shortModel(topModel) : '') + '">' +
          esc(topModel ? shortModel(topModel) : '-') + '</td>' +
        '<td class="num">' + fmtInt(b.events) + '</td>' +
        '<td class="num"><b>' + fmtCompact(b.total) + '</b></td></tr>';
    }).join('') || '<tr><td colspan="5" class="muted">无数据</td></tr>';

    var trs = $('sessBody').querySelectorAll('tr[data-sess]');
    for (var i = 0; i < trs.length; i++) {
      trs[i].addEventListener('click', function () {
        openSession(this.getAttribute('data-sess'), meta, agg);
      });
    }
    if (SEL_SESSION && meta[SEL_SESSION]) openSession(SEL_SESSION, meta, agg);
    else $('sessDetail').classList.add('hide');
  }

  function openSession(key, meta, agg) {
    SEL_SESSION = key;
    var b = agg.bySession[key];
    if (!b) return;
    var m = meta[key] || { models: {} };
    var models = Object.keys(m.models).sort(function (x, y) { return m.models[y] - m.models[x]; });
    var max = models.length ? m.models[models[0]] : 1;
    var rows = models.map(function (a) {
      var w = m.models[a] / max * 100;
      return '<div style="display:flex;align-items:center;gap:8px;margin:5px 0;font-size:12.5px">' +
        '<span style="flex:0 0 200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' +
          esc(shortModel(a)) + '">' + esc(shortModel(a)) + '</span>' +
        '<span class="meter" style="flex:1 1 auto;height:8px;border-radius:2px">' +
          '<span style="display:block;width:' + w.toFixed(1) + '%;height:8px;border-radius:2px;background:var(--blue)"></span></span>' +
        '<span style="flex:0 0 90px;text-align:right;font-variant-numeric:tabular-nums">' +
          fmtCompact(m.models[a]) + '</span></div>';
    }).join('');

    $('sessDetail').innerHTML =
      '<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">' +
      '<b style="font-size:13px">' + esc(sessionLabel(key)) + '</b>' +
      (key === WBToken.UNKNOWN_SESSION ? '' :
        '<span class="muted" style="font-size:11px;font-family:ui-monospace,monospace">' + esc(key) + '</span>') +
      '<span class="muted" style="font-size:12px">' + fmtInt(b.events) + ' 次 · ' + fmtCompact(b.total) + ' token</span>' +
      '<span style="flex:1 1 auto"></span>' +
      '<button class="sm" id="closeSess">收起</button></div>' + rows;
    $('sessDetail').classList.remove('hide');
    $('closeSess').addEventListener('click', function () {
      SEL_SESSION = null;
      $('sessDetail').classList.add('hide');
    });
  }

  // ------------------------------------------------------------------ 每日明细
  function renderDays(agg) {
    var grand = agg.totals.total || 1;
    var days = Object.keys(agg.byDay).sort().reverse();
    $('dayBody').innerHTML = days.map(function (d) {
      var b = agg.byDay[d];
      return '<tr><td>' + d + '</td>' +
        '<td class="num">' + fmtInt(b.events) + '</td>' +
        '<td class="num">' + fmtCompact(b.tin) + '</td>' +
        '<td class="num">' + fmtCompact(b.tout) + '</td>' +
        '<td class="num">' + fmtCompact(b.cread) + '</td>' +
        '<td class="num"><b>' + fmtCompact(b.total) + '</b></td>' +
        '<td class="num">' + fmtPct(b.total / grand) + '</td></tr>';
    }).join('') || '<tr><td colspan="7" class="muted">无数据</td></tr>';
  }

  // ------------------------------------------------------------------ 覆盖诊断
  function renderCover() {
    var keys = Object.keys(COVER).sort(function (a, b) { return COVER[b].token - COVER[a].token; });
    $('covBody').innerHTML = keys.map(function (k) {
      var v = COVER[k];
      var empty = v.events === 0;
      return '<tr' + (empty ? ' class="muted"' : '') + '>' +
        '<td title="' + esc(k) + '">' + esc(k) +
          (empty ? ' <span class="muted">（无用量记录）</span>' : '') + '</td>' +
        '<td class="num">' + v.files + '</td>' +
        '<td class="num">' + fmtInt(v.events) + '</td>' +
        '<td class="num">' + fmtCompact(v.token) + '</td></tr>';
    }).join('') || '<tr><td colspan="4" class="muted">无数据</td></tr>';
  }

  // ------------------------------------------------------------------ 自检
  function renderCheck(agg) {
    var out = $('checkOut');
    var html = '';

    // 1) 聚合自洽性（始终检查当前区间）
    var total = agg.totals.total;
    var sums = {
      byDay: Object.keys(agg.byDay).reduce(function (s, k) { return s + agg.byDay[k].total; }, 0),
      byModel: Object.keys(agg.byModel).reduce(function (s, k) { return s + agg.byModel[k].total; }, 0),
      byHour: Object.keys(agg.byHour).reduce(function (s, k) { return s + agg.byHour[k].total; }, 0),
      bySession: Object.keys(agg.bySession).reduce(function (s, k) { return s + agg.bySession[k].total; }, 0),
      byDayModel: Object.keys(agg.byDayModel).reduce(function (s, k) { return s + agg.byDayModel[k]; }, 0)
    };
    var okAll = Object.keys(sums).every(function (k) { return sums[k] === total; });
    html += '<div class="row"><span>各分类合计与总计一致</span>' +
            (okAll ? '<span class="ok">通过</span>'
                   : '<span class="ng">不一致：' + esc(JSON.stringify(sums)) + '</span>') + '</div>';

    // 2) 覆盖概览：只报「读到了多少」，不猜「是否漏了」——
    //    因为目录本来就可能没有用量记录，报警只会制造假警报。
    var dirs = Object.keys(COVER);
    var withEvents = dirs.filter(function (k) { return COVER[k].events > 0; });
    html += '<div class="row"><span>读取覆盖</span><span>读到 ' + dirs.length +
      ' 个项目目录，其中 ' + withEvents.length + ' 个有用量记录' +
      '<span class="muted">（若某个目录应该有记录却没有，请单独再选它一次）</span></span></div>';

    // 3) 子代理
    if (META.subMode) {
      var extra = '';
      if (META.subMode === 'included') {
        extra = META.subIncluded ? '（' + META.subIncluded + ' 个文件）' : '';
      } else if (META.subExcluded) {
        extra = '（' + META.subExcluded + ' 个文件）';
      }
      html += '<div class="row"><span>子代理会话</span><span>' +
        (META.subMode === 'included'
          ? '已计入' + extra + '，数字会偏高'
          : '已排除' + extra) + '</span></div>';
    }

    out.innerHTML = html;
  }

  // ------------------------------------------------------------------ 本地服务模式
  // 用本地服务打开时（http 协议），页面加载即自动取数、点刷新即最新，无需选文件。
  // 用文件方式打开（file://）时浏览器无法读本地目录，退化为手动选择。
  var SRC = 'unknown';

  function setSrc(text, dot) {
    $('srcText').innerHTML = text;
    $('srcDot').className = 'sdot ' + (dot || '');
  }

  /** 服务端紧凑数组 → 事件对象（字段顺序必须与 token-usage-server.py 一致）。 */
  function decodePayload(d) {
    var A = d.agents, S = d.sessions, P = d.projects, rows = d.rows;
    var n = rows.length, evs = new Array(n);
    for (var i = 0; i < n; i++) {
      var r = rows[i];
      evs[i] = {
        ts: r[0],
        agent: A[r[1]],
        session: r[2] >= 0 ? S[r[2]] : null,
        project: P[r[3]],
        tin: r[4], cread: r[5], tout: r[6], cwrite: r[7], reasoning: r[8]
      };
    }
    return evs;
  }

  async function loadFromServer() {
    var includeSub = $('includeSub').checked;
    setSrc('正在读取本机数据…', '');
    $('refreshBtn').disabled = true;
    var t0 = performance.now();
    try {
      var res = await fetch('/api/events?sub=' + (includeSub ? 1 : 0), { cache: 'no-store' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      var data = await res.json();
      var evs = decodePayload(data);
      var ms = Math.round(performance.now() - t0);

      EVENTS = evs;
      TITLES = Object.create(null);
      PREVIEWS = Object.create(null);
      var tArr = data.titles || [];
      for (var ti = 0; ti < tArr.length; ti++) {
        if (tArr[ti]) TITLES[data.sessions[ti]] = tArr[ti];
      }

      // 覆盖诊断：文件数由服务端提供，调用次数与 token 由事件累计
      COVER = {};
      var fbp = data.files_by_project || {};
      Object.keys(fbp).forEach(function (p) {
        COVER[p] = { files: fbp[p], events: 0, token: 0 };
      });
      evs.forEach(function (e) {
        var p = e.project || '(未知)';
        if (!COVER[p]) COVER[p] = { files: 0, events: 0, token: 0 };
        COVER[p].events += 1;
        COVER[p].token += e.tin + e.cread + e.tout;
      });

      var totalFiles = 0;
      Object.keys(fbp).forEach(function (p) { totalFiles += fbp[p]; });
      META = {
        files: totalFiles,
        skipped: 0,
        ms: ms,
        subExcluded: 0,
        subIncluded: 0,
        subMode: data.include_subagents ? 'included' : 'excluded',
        server: true
      };

      // 额度/积分另取一次：与事件解耦，未配置联网时完全不发额外请求。
      // 必须放在 setSrc 之前——状态条说"已连接"时，概览区的数据应该已经齐了。
      // 之后由 startQuotaAutoRefresh 每 60 秒续刷（只刷这一个接口）。
      await refreshQuota();

      SRC = 'server';
      setSrc('已读取本机记录 · <b>' + fmtInt(evs.length) + '</b> 次调用 · 更新于 ' +
             esc(data.generated_at || '') + ' · 用时 ' + (ms / 1000).toFixed(1) + ' 秒' +
             srcSummary(data), 'live');
      $('fileMode').classList.add('hide');
      $('progress').classList.remove('on');

      RANGES = Object.create(null);      // 新数据：所有分类的时间范围重置
      SEL_SESSION = null;
      syncRangeButtons();
      syncDateInputs();
      renderAll();
    } catch (err) {
      SRC = 'file';
      setSrc('应用未运行（' + esc(err.message) + '）· 请手动选择文件夹', 'file');
      $('fileMode').classList.remove('hide');
      if (!EVENTS.length) $('result').classList.add('hide');
    } finally {
      $('refreshBtn').disabled = false;
    }
    // 服务模式下启动额度自动刷新（文件模式没有 /api/quota，不起定时器）
    if (SRC === 'server') startQuotaAutoRefresh();
  }

  // ------------------------------------------------------------------ 分类切换
  // 页面很长，一次性全展示要滑很久。这里把区块分组，点分类只看该组，
  // 其余隐藏；切换后回到顶部，用户从该分类的第一屏开始往下看。
  var CAT = 'overview';
  var CATS = ['overview', 'trend', 'models', 'sessions', 'detail'];

  function applyCat() {
    // 注意：导航按钮本身也带 data-cat，必须排除掉，否则会把自己隐藏
    var nodes = document.querySelectorAll('[data-cat]:not(.cat)');
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].classList.contains('cat')) continue;
      var on = nodes[i].getAttribute('data-cat') === CAT;
      nodes[i].classList.toggle('hide', !on);
    }
    var btns = document.querySelectorAll('.catnav .cat');
    for (var j = 0; j < btns.length; j++) {
      btns[j].classList.toggle('on', btns[j].getAttribute('data-cat') === CAT);
    }
  }

  /** 隐藏状态下布局宽度为 0，图表「滚到最右（最近）」会失效，切回来时补一次。 */
  function reflowCharts() {
    fitHeatmap();
    var sx = $('scrollx');
    if (sx) sx.scrollLeft = sx.scrollWidth;
    fitRows();
    bindRowSnap();
  }

  /**
   * 把「限高滚动区」的高度改成行高的整数倍 —— 恰好显示 n 行整，
   * 不出现底部被切掉半行的情况。行高按渲染结果实测，不写死。
   * 隐藏状态下测到 0，会被跳过；切回该分类时 reflowCharts 会再跑一次。
   */
  function fitRows() {
    if (!document.querySelectorAll) return;
    var boxes = document.querySelectorAll('.tbl-b, .scroll');
    for (var i = 0; i < boxes.length; i++) {
      var b = boxes[i];
      var tr = b.querySelector ? b.querySelector('tbody tr') : null;
      if (!tr) continue;
      var rh = tr.offsetHeight || 0;
      if (rh <= 0) continue;
      var want = parseFloat(b.style.maxHeight) || 0;
      if (!want && window.getComputedStyle) {
        var cs = window.getComputedStyle(b);
        want = cs ? (parseFloat(cs.maxHeight) || 0) : 0;
      }
      if (!want) continue;
      var rows = Math.max(3, Math.round(want / rh));
      b.style.maxHeight = (rows * rh) + 'px';
    }
  }

  /** 滚动停稳后吸附到整行（110ms 防抖，避免和惯性滚动打架）。 */
  function bindRowSnap() {
    if (!document.querySelectorAll) return;
    var boxes = document.querySelectorAll('.tbl-b, .scroll');
    for (var i = 0; i < boxes.length; i++) {
      var b = boxes[i];
      if (b._snapBound) continue;
      b._snapBound = true;
      b.addEventListener('scroll', function () {
        var el = this;
        clearTimeout(el._snapT);
        el._snapT = setTimeout(function () {
          var tr = el.querySelector ? el.querySelector('tbody tr') : null;
          var rh = tr ? (tr.offsetHeight || 0) : 0;
          if (rh <= 0) return;
          var max = (el.scrollHeight || 0) - (el.clientHeight || 0);
          var y = Math.round(el.scrollTop / rh) * rh;
          if (y > max) y = max;
          if (y < 0) y = 0;
          if (Math.abs(y - el.scrollTop) > 1 && el.scrollTo) {
            el.scrollTo({ top: y, behavior: 'smooth' });
          }
        }, 110);
      });
    }
  }

  function setCat(c) {
    if (CATS.indexOf(c) < 0) return;
    CAT = c;
    dpClose();                     // 切分类会换时间范围，滚轮跟着收起
    applyCat();
    // 每个分类有自己的时间范围：切过去要先把筛选器同步成它的状态，再按它的范围重算
    syncRangeButtons();
    syncDateInputs();
    if (EVENTS.length) renderAll(); else reflowCharts();
    if (window.scrollTo) window.scrollTo(0, 0);
  }

  function bindCatNav() {
    var btns = document.querySelectorAll('.catnav .cat');
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener('click', function () {
        setCat(this.getAttribute('data-cat'));
      });
    }
  }

  $('refreshBtn').addEventListener('click', function () {
    if (SRC === 'server') loadFromServer();
    else if (EVENTS.length) renderAll();          // 文件模式：重渲染当前数据
    else alert('当前没有数据，请先选择文件夹');
  });

  bindCatNav();
  applyCat();

  // 系统切换浅色/深色时，CSS 变量会自动生效；但趋势图与热力图的颜色是 JS 生成的，
  // 需要重绘一次才能跟上（尤其是热力图的对数色阶）。
  if (window.matchMedia) {
    var mq = window.matchMedia('(prefers-color-scheme: dark)');
    var onScheme = function () { if (EVENTS.length) renderAll(); };
    if (mq.addEventListener) mq.addEventListener('change', onScheme);
    else if (mq.addListener) mq.addListener(onScheme);
  }

  // 供端到端测试读取内部状态（只读，不参与业务逻辑）
  window.WBApp = {
    events: function () { return EVENTS; },
    hours: function () { return HOUR_DATA; },
    cat: function () { return CAT; },
    setCat: setCat,
    range: function (c) { return rangeOf(c || CAT); },
    // 合成额度数据并重绘：用于测「联网开启/失败」等真实服务上不出现的状态
    setQuota: function (q) { QUOTA = q; renderQuota(); },
    // 自动刷新定时器是否在跑（服务模式启动后应非 null；文件模式为 null）
    quotaTimer: function () { return _quotaTimer; },
    quotaRefreshMs: function () { return QUOTA_REFRESH_MS; }
  };

  (function boot() {
    if (/^https?:$/.test(location.protocol)) {
      // 只有本机地址才去连本地服务；被托管在公网（比赛演示等）时直接进浏览器内解析模式，
      // 否则会先打一次必然失败的 /api/events，界面上还留着 HTTP 错误码。
      var host = location.hostname;
      if (!host && location.href) {
        try { host = new URL(location.href).hostname; } catch (e) { host = ''; }
      }
      var isLocal = /^(127\.0\.0\.1|localhost|\[::1\])$/.test(host || '');
      if (isLocal) {
        loadFromServer();
      } else {
        enterWebDemo();
      }
    } else {
      SRC = 'file';
      setSrc('当前为文件模式（无法自动读取目录），请手动选择文件夹；' +
             '通过应用打开可自动读取最新数据', 'file');
      $('fileMode').classList.remove('hide');
    }
  })();

  /** 网页版（无本地服务）：数据在浏览器内解析，不联网、不上传。 */
  function enterWebDemo() {
    SRC = 'file';
    setSrc('网页版 · 数据在你的浏览器内解析，不会上传', 'file');
    var fm = $('fileMode');
    if (fm) fm.classList.remove('hide');
    var tip = $('webDemoTip');
    if (tip) tip.classList.remove('hide');
    // 额度与积分依赖本地服务，网页版拿不到，整块隐藏（留着只会显示一句"需要应用在运行"）
    var qs = $('quotaSec');
    if (qs) qs.classList.add('hide');
    var db = $('demoBtn');
    if (db && db.addEventListener) {
      if (!window.WB_DEMO) { db.classList.add('hide'); return; }
      db.addEventListener('click', loadDemo);
    }
  }

  /** 演示数据：把内置的虚构会话包装成 File，走与真实文件完全相同的解析链路。 */
  function loadDemo() {
    var demo = window.WB_DEMO;
    if (!demo || !demo.files || !demo.files.length) { alert('演示数据未加载'); return; }
    var files = demo.files.map(function (f) {
      var file = new File([f.lines.join('\n')], f.name, { type: 'application/x-ndjson' });
      // filePath() 优先读 __path，这样演示数据也能有像样的「项目目录」
      try { Object.defineProperty(file, '__path', { value: f.path }); }
      catch (e) { file.__path = f.path; }
      return file;
    });
    ACC = [];
    ingest(files);
  }

  // ------------------------------------------------------------------ 启动
})();
