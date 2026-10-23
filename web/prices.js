/* prices.js —— Token 单价表
 *
 * 由人维护 / 每周复核；**不预置任何未经核实的数字**。
 * 每条必须带 source_url + verified_at + confidence，否则计价引擎拒绝使用。
 *
 * confidence 取值：
 *   official        官方定价页
 *   official_x2     两个独立官方/一手来源一致
 *   third_party     第三方跟踪站（声称已对照官方页核实）
 *   secondary       新闻报道/二手来源
 *   user            你自己填写
 *
 * 计价规则要点（见 cost.js）：
 *   · 逐事件计价，不按聚合总量乘单价 —— 因为存在峰谷价与阶梯价
 *   · 缓存命中/缓存写入单独计价，二者通常远低于输入价
 *   · cache_write 为 0 表示该厂商不单独收取缓存写入费（新 token 已按未命中计）
 *   · rates 用 "flat" 表示无峰谷区分；用 "offpeak"/"peak" + peak_rule 表示峰谷价
 */
window.WB_PRICES = {
  "schema": 2,
  "generated_at": "2026-10-22",
  "unit": 1000000,
  "note": "单价随厂商调整频繁。金额仅作「API 等价价值」参考，不等于你的实际支出。",

  // 跨币种合并需要汇率；留 null 表示不合并（只分币种小计），避免我替你编一个汇率
  "fx": {
    "USD_per_CNY": null,
    "as_of": null,
    "note": "想看到合并总额时填这里，例如 0.141 表示 1 CNY = 0.141 USD。请自行核对汇率。"
  },

  "models": {
    "deepseek-v4.1-flash": {
      "display": "DeepSeek V4.1 Flash",
      "vendor": "DeepSeek",
      "currency": "USD",
      "aliases": [
        "deepseek-v4.1-flash", "deepseek-flash",
        "deepseek-v4-flash", "deepseek-v4-flash-vision-exp",
        "deepseek-ai/deepseek-v4.1-flash"
      ],
      "rates": {
        "offpeak": { "input": 0.15, "cache_read": 0.003, "cache_write": 0, "output": 0.60 },
        "peak":    { "input": 0.30, "cache_read": 0.006, "cache_write": 0, "output": 1.20 }
      },
      "peak_rule": {
        "tz": "Asia/Shanghai",
        "days": [1, 2, 3, 4, 5],
        "windows": [["09:00", "12:00"], ["14:00", "18:00"]]
      },
      "source_url": "https://deepseek.ai/pricing",
      "verified_at": "2026-09-18",
      "confidence": "third_party",
      "note": "两家独立跟踪站数字一致（均称已对照官方定价页核实）。官方中文文档的 CNY 表与此对不上，疑为不同版本价目，采用前请自行核对。"
    },

    "deepseek-v4-pro": {
      "display": "DeepSeek V4 Pro",
      "vendor": "DeepSeek",
      "currency": "USD",
      "aliases": ["deepseek-v4-pro", "deepseek-v4-pro-0813"],
      "rates": {
        "offpeak": { "input": 0.66, "cache_read": 0.022, "cache_write": 0, "output": 1.98 },
        "peak":    { "input": 1.32, "cache_read": 0.044, "cache_write": 0, "output": 3.96 }
      },
      "peak_rule": {
        "tz": "Asia/Shanghai",
        "days": [1, 2, 3, 4, 5],
        "windows": [["09:00", "12:00"], ["14:00", "18:00"]]
      },
      "source_url": "https://deepseek.ai/pricing",
      "verified_at": "2026-09-18",
      "confidence": "third_party",
      "note": "同上，USD 表。"
    },

    "glm-5.3": {
      "display": "GLM-5.3",
      "vendor": "智谱",
      "currency": "CNY",
      "aliases": ["glm-5.3", "zhipu/glm-5.3", "glm-5-3"],
      "rates": {
        "flat": { "input": 8, "cache_read": 2, "cache_write": 0, "output": 28 }
      },
      "source_url": "https://bigmodel.cn/pricing?tab=api",
      "verified_at": "2026-10-22",
      "confidence": "official_x2",
      "note": "阿里云百炼（智谱原厂直供）与智谱 BigModel 均为 输入8 / 输出28 / 缓存命中2（元/百万），1M 上下文，无峰谷区分。"
    },

    "glm-5.3-flash": {
      "display": "GLM-5.3-Flash",
      "vendor": "智谱",
      "currency": "CNY",
      "aliases": ["glm-5.3-flash"],
      "rates": {
        "flat": { "input": 0.8, "cache_read": 0.23, "cache_write": 0, "output": 2.8 }
      },
      "source_url": "https://help.aliyun.com/document_detail/2586397.html",
      "verified_at": "2026-10-22",
      "confidence": "official_x2",
      "note": "百炼价格表与新闻报道一致（0.8 / 2.8 / 缓存命中 0.23）。"
    },

    "glm-5.2": {
      "display": "GLM-5.2",
      "vendor": "智谱",
      "currency": "CNY",
      "aliases": ["glm-5.2"],
      "rates": {
        "flat": { "input": 8, "cache_read": 2, "cache_write": 0, "output": 28 }
      },
      "source_url": "https://help.aliyun.com/document_detail/2586397.html",
      "verified_at": "2026-10-22",
      "confidence": "official",
      "note": "阿里云百炼：glm-5.2 统一定价 8 / 28（元/百万），不分思考/非思考。缓存命中 2 元系参照同页 glm-5.3 档位推定，属估算。"
    },

    "step-5-preview": {
      "display": "Step 5 Preview",
      "vendor": "阶跃星辰",
      "currency": "CNY",
      "aliases": ["step-5-preview"],
      "rates": {
        "flat": { "input": 7, "cache_read": 0.7, "cache_write": 0, "output": 20 }
      },
      "source_url": "https://platform.stepfun.com/docs/guides/pricing/details",
      "verified_at": "2026-10-23",
      "confidence": "official_x2",
      "note": "官网定价页与调价公告一致。缓存命中价 2026-10-14 00:30 起由 0.35 上调到 0.70 元，本表用新价；此前历史消耗按新价估算会略偏高。WorkBuddy 于 2026-10-12 ~ 10-25 独家限免该模型（消耗倍率 0.00x）；本表按 API 单价统一折算，不区分是否限免。"
    },

    "mimo-v2.5-pro": {
      "display": "Xiaomi MiMo-V2.5-Pro",
      "vendor": "小米",
      "currency": "USD",
      "aliases": ["mimo-v2.5-pro", "xiaomi/mimo-v2.5-pro"],
      "rates": {
        "flat": { "input": 0.435, "cache_read": 0.0036, "cache_write": 0, "output": 0.87 }
      },
      "source_url": "https://www.requesty.ai/models/xiaomi/mimo-v2.5-pro",
      "verified_at": "2026-10-21",
      "confidence": "third_party",
      "note": "Requesty 与 modelpricelab 均为 0.435 / 0.87、缓存读 0.0036；computecomparison 称该模型未公布缓存价。缓存读价存疑。"
    },

    "minimax-m3.1-flash": {
      "display": "MiniMax M3.1 Flash",
      "vendor": "MiniMax",
      "currency": "USD",
      "aliases": ["minimax-m3.1-flash", "space-bunny"],
      "rates": {
        "flat": { "input": 0.15, "cache_read": 0.015, "cache_write": 0, "output": 0.60 }
      },
      "source_url": "https://www.minimax.io/blog/minimax-m31f",
      "verified_at": "2026-10-22",
      "confidence": "official_x2",
      "note": "MiniMax 官方博客 $0.15 / $0.60 / 缓存 0.015，与中文报道 ¥1 / ¥4 / ¥0.1 一致。space-bunny 是 M3.1 Flash 的早期匿名版本（官方原文），故并入本条。"
    },

    "kimi-k3": {
      "display": "Kimi K3",
      "vendor": "月之暗面",
      "currency": "CNY",
      "aliases": ["kimi-k3", "kimi-k3-1"],
      "rates": {
        "flat": { "input": 20, "cache_read": 2, "cache_write": 20, "output": 100 }
      },
      "source_url": "https://www.zhengguannews.cn/zghnews/426612.html",
      "verified_at": "2026-10-22",
      "confidence": "secondary",
      "note": "仅二手报道来源（称引自官网报价）。Kimi 缓存写入单独计价 20 元/百万，这一点与多数厂商不同，值得复核。"
    }
  },

  // 明确「算不了」的类别，避免被静默按 0 处理
  "unpriced": {
    "auto": "路由别名：底层模型被隐藏，无法给出单一单价",
    "fast-model": "路由别名：按档位路由到不同底层模型",
    "balanced-model": "路由别名：按档位路由到不同底层模型",
    "deep-model": "路由别名：按档位路由到不同底层模型",
    "hy4-preview": "腾讯混元内部代号，无公开 API 定价",
    "hy4-preview-f": "腾讯混元内部代号，无公开 API 定价",
    "hy3": "腾讯混元内部代号，无公开 API 定价",
    "intern-s2": "上海 AI 实验室书生系列，未核实到公开 API 定价",
    "intern-s2-preview-397b": "同上，397B 预览版",
    "atria-dawn": "内部/自建模型代号，无公开定价",
    "atria-dawn-preview": "同上，预览版",
    "minimax-m3": "仅有相对描述（M3.1 比 M3 便宜约 50%），无确切绝对值",
    "glm-5.2-a": "疑似 glm-5.2 内部变体，无法确认是否同一计费型号",
    "gpt-6-luna": "未核实到 OpenAI 该型号的公开定价页",
    "gpt-5.6-luna": "同上",
    "kimi-k2.5": "月之暗面旧代模型，未核实到当前官方价",
    "kimi-k2.6": "同上",
    "kimi-k2.7": "同上",
    "codewise-completions": "补全类模型，计费口径与对话模型不同",
    "codewise-rewrite": "同上",
    "codewise-jump": "同上",
    "codewise-nes-a4-027-aide": "同上",
    "agents-a1": "内部代号，对应模型未知",
    "qwen3:8b": "本地 Ollama 模型，无 API 单价（成本为电费与折旧）",
    "codex-auto-review": "Codex 内部审查模型，无独立定价",
    "cursor-grok-4.6-medium": "Cursor 平台内模型，按订阅计，无独立 token 单价"
  },

  /* 已下架：官方明确说明退役、请求被路由到新模型。
     与「未定价」语义不同——前者是模型不存在了，后者是我没查到价。 */
  "retired": {
    "deepseek-v4-flash": "已被 V4.1-Flash 取代，请求路由到 V4.1-Flash 并按 Flash 价计费（官方文档）",
    "deepseek-v4-flash-vision-exp": "同上，实验版已退役"
  },

  /* 按约定不核定的范围，列出来让「为什么没价」可查 */
  "policy": {
    "min_call_share": 0.001,
    "note": "总调用量占比不超过 0.1% 的模型不予核定单价：影响可忽略，维护成本不划算。",
    "below_threshold": [
      "cursor-grok-4.6-medium", "qwen3:8b", "codex-auto-review",
      "agents-a1", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"
    ]
  }
};
