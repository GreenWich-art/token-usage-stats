/*!
 * cost.js —— 逐事件成本计价引擎
 *
 * 为什么必须逐事件算：
 *   ① 峰谷价：同一个模型一天内有两个价（DeepSeek 北京时间 9-12、14-18 为高峰）
 *   ② 阶梯价：部分模型超阈值后单价上涨
 *   ③ 别名重定向：退役模型名会被路由到新模型并按新价计费
 *   「按模型总量 × 单价」在以上任一情形下都是错的。
 *
 * 口径：算出来的是「API 等价价值」——同样 token 若按厂商官网价直接调用要花多少。
 *       订阅套餐内的实际边际支出通常是 0，不要与这个数字混为一谈。
 *
 * 与 token-semantics.js 一样支持浏览器 + Node（UMD）。
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.WBPrice = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var TABLE = null;
  var ALIAS = Object.create(null);   // 规范化别名 → 价格条目 key
  var UNPRICED = Object.create(null);

  var WEEKDAY_IX = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };

  /** 把模型标识规范化用于价格匹配。 */
  function normalizeModelId(raw) {
    var id = String(raw == null ? '' : raw).trim();
    var free = false;
    // 1) 客户端前缀，如 workbuddy-model: / codex-model: / deepseek-model:
    //    与自定义标记可能叠加，例如 workbuddy-model:custom-local:glm-5.3，故循环剥离
    var guard = 0;
    while (guard++ < 4) {
      var m = id.match(/^[a-z][a-z0-9_]*-model:/i);
      if (m) { id = id.slice(m[0].length); continue; }
      if (id.toLowerCase().indexOf('custom-local:') === 0) {
        id = id.slice('custom-local:'.length);
        continue;
      }
      break;
    }
    // 2) 供应商路径前缀，如 deepseek-ai/deepseek-v4.1-flash
    var slash = id.lastIndexOf('/');
    if (slash >= 0) id = id.slice(slash + 1);
    // 3) 免费额度后缀
    if (/-free$/i.test(id)) {
      free = true;
      id = id.slice(0, -5);
    }
    return { id: id.toLowerCase(), free: free };
  }

  function init(table) {
    TABLE = table || (typeof window !== 'undefined' ? window.WB_PRICES : null);
    ALIAS = Object.create(null);
    UNPRICED = Object.create(null);
    if (!TABLE) return false;
    var models = TABLE.models || {};
    Object.keys(models).forEach(function (key) {
      var m = models[key];
      ALIAS[key.toLowerCase()] = key;
      (m.aliases || []).forEach(function (a) {
        var n = normalizeModelId(a).id;
        if (!ALIAS[n]) ALIAS[n] = key;
      });
    });
    Object.keys(TABLE.unpriced || {}).forEach(function (k) {
      UNPRICED[normalizeModelId(k).id] = TABLE.unpriced[k];
    });
    return true;
  }

  // ---------------------------------------------------------------- 时区/峰谷
  var _fmtCache = Object.create(null);
  var _bucketCache = Object.create(null);

  function tzParts(tsMs, tz) {
    var bucket = Math.floor(tsMs / 3600000);   // 按小时缓存，避免每条事件都做 Intl 格式化
    var ck = tz + '|' + bucket;
    if (_bucketCache[ck]) return _bucketCache[ck];
    var fmt = _fmtCache[tz];
    if (!fmt) {
      fmt = new Intl.DateTimeFormat('en-US', {
        timeZone: tz, weekday: 'short', hour: '2-digit', minute: '2-digit',
        hourCycle: 'h23'
      });
      _fmtCache[tz] = fmt;
    }
    var parts = fmt.formatToParts(new Date(tsMs));
    var wd = '', hh = 0, mm = 0;
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i];
      if (p.type === 'weekday') wd = p.value;
      else if (p.type === 'hour') hh = parseInt(p.value, 10) % 24;
      else if (p.type === 'minute') mm = parseInt(p.value, 10);
    }
    var out = { dow: WEEKDAY_IX[wd] === undefined ? -1 : WEEKDAY_IX[wd], hh: hh, mm: mm };
    _bucketCache[ck] = out;
    return out;
  }

  function toMin(hhmm) {
    var a = String(hhmm).split(':');
    return (parseInt(a[0], 10) || 0) * 60 + (parseInt(a[1], 10) || 0);
  }

  /** 该时刻是否落在高峰窗口内。规则缺失视为无峰谷。 */
  function isPeak(tsMs, rule) {
    if (!rule || !rule.windows || !rule.windows.length) return false;
    var t = tzParts(tsMs, rule.tz || 'UTC');
    if (rule.days && rule.days.indexOf(t.dow) < 0) return false;
    var m = t.hh * 60 + t.mm;
    for (var i = 0; i < rule.windows.length; i++) {
      var w = rule.windows[i];
      if (m >= toMin(w[0]) && m < toMin(w[1])) return true;
    }
    return false;
  }

  // ---------------------------------------------------------------- 计价
  /**
   * 单个事件的成本。
   * @returns {{status:string, amount:number, currency:string, key:string|null, tier:string|null, why:string|null}}
   *   status: priced | free | unpriced | no_rule
   */
  function costOf(ev) {
    var n = normalizeModelId(ev.agent || ev.model);
    if (n.free) {
      return { status: 'free', amount: 0, currency: '', key: null, tier: null, why: '免费额度后缀 -free' };
    }
    var key = ALIAS[n.id];
    if (!key) {
      var why = UNPRICED[n.id] || '价格表未收录';
      return { status: 'unpriced', amount: 0, currency: '', key: null, tier: null, why: why };
    }
    var m = TABLE.models[key];
    var rates = m.rates || {};
    var tier, rate;
    if (rates.flat) {
      tier = 'flat'; rate = rates.flat;
    } else {
      var peak = isPeak(ev.ts, m.peak_rule);
      tier = peak ? 'peak' : 'offpeak';
      rate = rates[tier] || rates.offpeak;
    }
    if (!rate) {
      return { status: 'no_rule', amount: 0, currency: m.currency || '', key: key, tier: null, why: '价格条目缺少可用档位' };
    }
    var unit = TABLE.unit || 1000000;
    var amount =
      (ev.tin || 0) / unit * (rate.input || 0) +
      (ev.cread || 0) / unit * (rate.cache_read || 0) +
      (ev.cwrite || 0) / unit * (rate.cache_write || 0) +
      (ev.tout || 0) / unit * (rate.output || 0);
    return { status: 'priced', amount: amount, currency: m.currency || '',
             key: key, tier: tier, why: null };
  }

  /** 批量汇总：按模型 / 按币种 / 未定价清单。 */
  function summarize(events) {
    var byAgent = Object.create(null);
    var byCurrency = Object.create(null);
    var unpriced = Object.create(null);
    var peakEvents = 0, offpeakEvents = 0;

    for (var i = 0; i < events.length; i++) {
      var ev = events[i];
      var r = costOf(ev);
      var a = ev.agent;
      if (!byAgent[a]) {
        byAgent[a] = { amount: 0, currency: '', status: r.status, tier: null, key: r.key, tokens: 0 };
      }
      var s = byAgent[a];
      s.tokens += (ev.tin || 0) + (ev.cread || 0) + (ev.tout || 0) + (ev.cwrite || 0);
      if (r.status === 'priced') {
        s.amount += r.amount;
        s.currency = r.currency;
        if (r.tier === 'peak') peakEvents++; else if (r.tier === 'offpeak') offpeakEvents++;
      } else if (r.status === 'unpriced' || r.status === 'no_rule') {
        s.status = 'unpriced';
        if (!unpriced[a]) unpriced[a] = { why: r.why, tokens: 0 };
        unpriced[a].tokens += (ev.tin || 0) + (ev.cread || 0) + (ev.tout || 0);
      }
    }
    Object.keys(byAgent).forEach(function (a) {
      var s = byAgent[a];
      if (s.status === 'priced' && s.currency) {
        byCurrency[s.currency] = (byCurrency[s.currency] || 0) + s.amount;
      }
    });
    return {
      byAgent: byAgent,
      byCurrency: byCurrency,
      unpriced: unpriced,
      peakEvents: peakEvents,
      offpeakEvents: offpeakEvents
    };
  }

  /** 跨币种合并总额；未配置汇率返回 null（不猜）。 */
  function mergeTotal(byCurrency) {
    var fx = (TABLE && TABLE.fx) || {};
    var rate = Number(fx.USD_per_CNY);
    if (!rate) return null;
    var total = 0;
    Object.keys(byCurrency).forEach(function (c) {
      if (c === 'CNY') total += byCurrency[c] * rate;
      else if (c === 'USD') total += byCurrency[c];
      else total += 0;   // 未知币种不计入合并
    });
    return { amount: total, currency: 'USD' };
  }

  function meta(key) { return (TABLE && TABLE.models[key]) || null; }

  return {
    init: init,
    costOf: costOf,
    summarize: summarize,
    mergeTotal: mergeTotal,
    meta: meta,
    isPeak: isPeak,
    normalizeModelId: normalizeModelId
  };
});
