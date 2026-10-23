/*!
 * token-semantics.js —— WorkBuddy / Agent HUD Token 计量语义 · JS 参考移植版
 *
 * 与 Python 参考实现 wb_token_semantics.py v1.0.0 逐条对齐。
 * 任何改动必须同时满足：
 *   ① Python 版 test_semantics.py 四关全过
 *   ② 本文件用 verify.js 跑出与 golden_daily.json 逐日相等
 *
 * ═══════════════════════════════════════════════════════════════
 * 三条口径铁律（与 Python 版同源，均由 HUD 完好窗口实证）
 *   R1 事件 = 任何带 providerData.usage 的 JSON 行
 *      —— 不是 assistant 消息；约 90% 用量落在 function_call 行
 *   R2 inputTokens 已含 cached_tokens
 *      tin = inputTokens - cached_tokens ；cread = cached_tokens
 *   R3 outputTokens 已含 reasoning_tokens
 *      tout = outputTokens ；reasoning 不单列
 *      总量 = tin + cread + tout + cwrite + reasoning
 * ═══════════════════════════════════════════════════════════════
 *
 * 与 Python 版的唯一差异（有意为之，为了可移植）：
 *   会话去重按「文件名（会话 UUID）」而不是 realpath。
 *   浏览器无法解析符号链接，而同一会话的符号链接副本文件名相同，
 *   故按文件名去重等价且更稳。verify.js 会验证这一等价性。
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.WBToken = factory();
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var SEMANTICS_VERSION = '1.0.0';

  // 必须与 Python 版常量一致
  var TRANSCRIPT_USAGE_PATH = ['providerData', 'usage'];
  var TRANSCRIPT_TIME_PATH = ['timestamp'];
  var TRANSCRIPT_MODEL_PATH = ['providerData', 'model'];
  var REQUEST_MODEL_ID_PATH = ['providerData', 'requestModelId'];
  var CACHED_TOKENS_PATH = ['inputTokensDetails', 0, 'cached_tokens'];
  var REASONING_TOKENS_PATH = ['outputTokensDetails', 0, 'reasoning_tokens'];
  var CLIENT_PREFIX = 'workbuddy';
  var UNKNOWN_SESSION = '(unknown)';

  /** 按路径取值，任何一层缺失返回 null（不抛异常）。 */
  function dig(obj, path) {
    var cur = obj;
    for (var i = 0; i < path.length; i++) {
      if (cur === null || cur === undefined) return null;
      cur = cur[path[i]];
      if (cur === undefined) return null;
    }
    return cur === undefined ? null : cur;
  }

  /**
   * 把 transcript 的一行文本解析为 TokenEvent；不是用量行返回 null。
   * 与 Python parse_transcript_line() 逻辑一一对应。
   */
  function parseTranscriptLine(line, sessionId) {
    // 快速过滤：绝大多数行不含 usage（性能关键，428MB 数据靠它撑住）
    if (line.indexOf('"usage"') === -1) return null;

    var d;
    try {
      d = JSON.parse(line);
    } catch (e) {
      return null; // 会话写入中末行可能被截断，属正常情况
    }
    if (!d || typeof d !== 'object') return null;

    var usage = dig(d, TRANSCRIPT_USAGE_PATH);
    var ts = dig(d, TRANSCRIPT_TIME_PATH);
    if (!usage || ts === null || ts === undefined) return null;

    var tinGross = usage.inputTokens || 0;
    var cached = dig(usage, CACHED_TOKENS_PATH) || 0;
    var tout = usage.outputTokens || 0;

    // R2：inputTokens 含 cached → 净新增输入
    var tin = tinGross - cached;
    if (tin < 0) tin = 0; // 数据异常保护

    var model = dig(d, TRANSCRIPT_MODEL_PATH) || 'unknown';
    // agent 用 requestModelId（带 custom-local: 前缀可区分内置/自定义），与 Python 版一致
    var rid = String(dig(d, REQUEST_MODEL_ID_PATH) || model);
    var channel = rid.toLowerCase().indexOf('custom-local:') === 0 ? 'byok' : 'builtin';

    return {
      ts: Number(ts),
      session: sessionId || null,
      client: CLIENT_PREFIX,
      model: model,
      channel: channel,
      agent: CLIENT_PREFIX + '-model:' + rid,
      tin: tin,
      tout: tout,       // 已含 reasoning
      cread: cached,
      cwrite: 0,
      reasoning: 0,     // 不单列，避免与 tout 重复
      _rawIn: tinGross,
      _rawOut: tout
    };
  }

  /** 单事件总量；必须与 bucketTotal() 公式一致，否则分维度合计对不上总计。 */
  function eventTotal(ev) {
    return ev.tin + ev.cread + ev.tout + ev.cwrite + ev.reasoning;
  }

  function bucketTotal(b) {
    return b.tin + b.cread + b.tout + b.cwrite + b.reasoning;
  }

  /** 本地时区日期 YYYY-MM-DD（与 Python datetime.fromtimestamp 一致）。 */
  function localDate(tsMs) {
    var d = new Date(tsMs);
    var m = String(d.getMonth() + 1).padStart(2, '0');
    var day = String(d.getDate()).padStart(2, '0');
    return d.getFullYear() + '-' + m + '-' + day;
  }

  /** 会话标识；缺失归入 UNKNOWN_SESSION，不允许事件被静默丢弃。 */
  function sessionKey(ev) {
    return ev.session || UNKNOWN_SESSION;
  }

  function newBucket() {
    return { events: 0, tin: 0, tout: 0, cread: 0, cwrite: 0, reasoning: 0, total: 0 };
  }

  function addTo(b, ev) {
    b.events += 1;
    b.tin += ev.tin;
    b.tout += ev.tout;
    b.cread += ev.cread;
    b.cwrite += ev.cwrite;
    b.reasoning += ev.reasoning;
  }

  function finishBucket(b) {
    b.total = b.tin + b.cread + b.tout + b.cwrite + b.reasoning;
    return b;
  }

  /**
   * 聚合。返回结构与 Python aggregate() 一致：
   *   events / totals / byDay / byModel / byDayModel / byHour / bySession / range
   * 字段名转成驼峰（JS 惯例），语义完全相同。
   */
  function aggregate(events) {
    var totals = newBucket();
    var byDay = Object.create(null);
    var byModel = Object.create(null);
    var byDayModel = Object.create(null);
    var byHour = Object.create(null);
    var bySession = Object.create(null);

    var n = 0;
    var tsMin = null;
    var tsMax = null;

    for (var i = 0; i < events.length; i++) {
      var ev = events[i];
      n += 1;
      var day = localDate(ev.ts);
      var hour = new Date(ev.ts).getHours();
      var agent = ev.agent;

      if (tsMin === null || ev.ts < tsMin) tsMin = ev.ts;
      if (tsMax === null || ev.ts > tsMax) tsMax = ev.ts;

      addTo(totals, ev);
      (byDay[day] || (byDay[day] = newBucket()));
      addTo(byDay[day], ev);

      (byModel[agent] || (byModel[agent] = newBucket()));
      addTo(byModel[agent], ev);

      (byHour[hour] || (byHour[hour] = newBucket()));
      addTo(byHour[hour], ev);

      var dmKey = day + '|' + agent;
      byDayModel[dmKey] = (byDayModel[dmKey] || 0) + eventTotal(ev);

      var sk = sessionKey(ev);
      (bySession[sk] || (bySession[sk] = newBucket()));
      addTo(bySession[sk], ev);
    }

    Object.keys(byDay).forEach(function (k) { finishBucket(byDay[k]); });
    Object.keys(byModel).forEach(function (k) { finishBucket(byModel[k]); });
    Object.keys(byHour).forEach(function (k) { finishBucket(byHour[k]); });
    Object.keys(bySession).forEach(function (k) { finishBucket(bySession[k]); });
    finishBucket(totals);

    return {
      events: n,
      totals: totals,
      byDay: byDay,
      byModel: byModel,
      byDayModel: byDayModel,
      byHour: byHour,
      bySession: bySession,
      range: tsMin === null ? null : [localDate(tsMin), localDate(tsMax)],
      semanticsVersion: SEMANTICS_VERSION
    };
  }

  // ---------------------------------------------------------------- 会话名称
  // 侧边栏显示的会话名来自 ai-title 行（字段 aiTitle），实测覆盖 82%；
  // 其余用首条用户消息兜底（需剥掉 system-reminder 与标签），再不行退回 UUID。
  var SYS_REMINDER_RE = /<system-reminder[\s\S]*?<\/system-reminder>/gi;
  var TAG_RE = /<[^>]{1,200}>/g;

  function parseTitleLine(line) {
    if (line.indexOf('"ai-title"') === -1) return null;
    var d;
    try { d = JSON.parse(line); } catch (e) { return null; }
    if (!d || d.type !== 'ai-title') return null;
    var t = (d.aiTitle || '').trim();
    return t || null;
  }

  function parseUserPreview(line, limit) {
    if (line.indexOf('"user"') === -1) return null;
    var d;
    try { d = JSON.parse(line); } catch (e) { return null; }
    if (!d || d.type !== 'message' || d.role !== 'user') return null;
    var content = d.content, texts = [];
    if (Array.isArray(content)) {
      for (var i = 0; i < content.length; i++) {
        var c = content[i];
        if (c && (c.type === 'input_text' || c.type === 'text')) texts.push(c.text || '');
      }
    } else if (typeof content === 'string') {
      texts.push(content);
    }
    if (!texts.length) return null;
    var s = texts.join('')
      .replace(SYS_REMINDER_RE, ' ')
      .replace(TAG_RE, ' ')
      .replace(/\s+/g, ' ')
      .trim();
    if (s.length < 2) return null;
    var lim = limit || 48;
    return s.length > lim ? s.slice(0, lim) + '…' : s;
  }

  /**
   * 从一行里尝试提取会话名称（ai-title 优先）。
   * 返回 {title} 或 {preview} 或 null —— 调用方自行决定优先级。
   */
  function parseMetaLine(line) {
    var t = parseTitleLine(line);
    if (t) return { title: t };
    var p = parseUserPreview(line);
    if (p) return { preview: p };
    return null;
  }

  /**
   * 流式逐行读取一个 File / Blob（浏览器 File 或 Node 均可，只要支持 stream()）。
   * 428MB 全量数据必须走流式，不能 readAsText 一次性读入。
   */
  async function forEachLine(blob, onLine) {
    var stream = blob.stream().pipeThrough(new TextDecoderStream());
    var reader = stream.getReader();
    var buf = '';
    while (true) {
      var chunk = await reader.read();
      if (chunk.done) break;
      buf += chunk.value;
      var idx;
      while ((idx = buf.indexOf('\n')) >= 0) {
        var line = buf.slice(0, idx);
        buf = buf.slice(idx + 1);
        if (line.length) onLine(line);
      }
    }
    if (buf.length) onLine(buf);
  }

  return {
    SEMANTICS_VERSION: SEMANTICS_VERSION,
    UNKNOWN_SESSION: UNKNOWN_SESSION,
    dig: dig,
    parseTranscriptLine: parseTranscriptLine,
    eventTotal: eventTotal,
    bucketTotal: bucketTotal,
    localDate: localDate,
    sessionKey: sessionKey,
    parseTitleLine: parseTitleLine,
    parseUserPreview: parseUserPreview,
    parseMetaLine: parseMetaLine,
    aggregate: aggregate,
    forEachLine: forEachLine
  };
});
