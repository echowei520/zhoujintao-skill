#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
周期定位取数器 v2 —— 为 zhoujintao 技能提供「当下各层周期的客观读数」。

数据源（2026-09-22 全量重写：免费、无密钥、无额度墙，实测在本机网络全部可达）
    1. 腾讯行情 qt.gtimg.cn   —— 外盘期货（COMEX黄金/白银/WTI/布伦特/伦敦铜）
                                  + 上证指数 + 标普500，实时读数与当日高低
    2. Frankfurter (欧央行)   —— EURUSD（美元周期代理，DXY 中权重 57.6%）
                                  + USDCNY（人民币直接读数），一次请求拿全年日度序列
    3. jsDelivr currency-api  —— BTC/ETH 现值 + 一年前检查点算同比 +
                                  月度采样近似一年区间高低
    4. alternative.me FNG     —— 加密恐惧贪婪指数
    5. 本地缓存兜底           —— 全部在线源失败时回读 .cache_cycle_data.json，
                                  并按【数据时效铁律】标注旧值（>7天仅供参考）。

用法
    python fetch_cycle_data.py               # Markdown 表
    python fetch_cycle_data.py --json        # 结构化 JSON
    python fetch_cycle_data.py --time-range 90d   # 缩短统计区间（frankfurter/jsdelivr 同步缩短）

注意
    任一指标失败不中断，在「取数失败项」中如实列出，不得编造数据。
    【数据时效铁律】读数超过 7 天必须在回答中标注为旧值，不得当现值引用。
"""

import argparse
import json
import os
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, ".cache_cycle_data.json")
STALE_DAYS = 7

HDRS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://gu.qq.com/"}


def http_get(url: str, timeout: int = 20):
    """GET 并返回 bytes。失败抛异常由调用方处理。"""
    req = urllib.request.Request(url, headers=HDRS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_get_json(url: str, timeout: int = 20):
    try:
        return json.loads(http_get(url, timeout))
    except Exception as exc:  # noqa: BLE001
        return {"__error": str(exc)[:160]}


# ----------------------------------------------------------------------------
# 源 1：腾讯行情（外盘期货 + 指数，实时）
# ----------------------------------------------------------------------------

# (显示名, 框架含义, 腾讯代码, 解析类型)
TENCENT_INDICATORS = [
    ("黄金(COMEX)", "康波萧条期核心避险资产、美元信用对冲", "hf_GC", "hf"),
    ("白银",        "贵金属第二观察窗，金银比信号",          "hf_SI", "hf"),
    ("原油WTI",     "滞胀与衰退的先行信号",                  "hf_CL", "hf"),
    ("布伦特",      "全球原油定价基准",                      "hf_OIL", "hf"),
    ("伦敦铜",      "工业需求与制造业中周期晴雨表",          "hf_CAD", "hf"),
    ("上证指数",    "中国库存周期与风险偏好直接观察窗",      "sh000001", "cn"),
    ("标普500",     "风险资产与全球风险偏好之锚",            "usINX", "us"),
]


def _split_tencent(body: bytes, code: str, kind: str):
    """解析腾讯行情返回。注意：外盘期货(hf_)字段用逗号分隔，股票/指数用 ~ 分隔。"""
    text = body.decode("gbk", errors="replace")
    marker = f'v_{code}="'
    i = text.find(marker)
    if i < 0:
        return None
    j = text.find('"', i + len(marker))
    raw = text[i + len(marker):j]
    return raw.split("," if kind == "hf" else "~")


def fetch_tencent(name, meaning, code, kind, failures):
    try:
        fields = _split_tencent(http_get(f"https://qt.gtimg.cn/q={code}"), code, kind)
        if not fields:
            raise ValueError("返回无该代码")
        if kind == "hf":
            last, chg, hi, lo = fields[0], fields[1], fields[4], fields[5]
            tdate = fields[12]
        elif kind == "cn":   # sh000001: 3=现价 32=涨跌% 33=最高 34=最低
            last, chg, hi, lo = fields[3], fields[32], fields[33], fields[34]
            tdate = fields[30][:8]
        else:                # usINX: 3=现价 32=涨跌% 33=最高 34=最低
            last, chg, hi, lo = fields[3], fields[32], fields[33], fields[34]
            tdate = fields[30][:10]
        return {"指标": name, "框架含义": meaning, "代码": code, "数据源": "tencent",
                "last": float(last), "change_pct": float(chg),
                "high": float(hi) if hi else None, "low": float(lo) if lo else None,
                "口径": "当日", "读数日期": tdate}
    except Exception as exc:  # noqa: BLE001
        failures.append({"指标": name, "原因": str(exc)[:120]})
        return None


# ----------------------------------------------------------------------------
# 源 2：Frankfurter（欧央行）——EURUSD / USDCNY 全年日度序列
# ----------------------------------------------------------------------------

def fetch_frankfurter(days_back: int, failures):
    out = []
    end = date.today()
    start = end - timedelta(days=days_back)
    url = (f"https://api.frankfurter.dev/v1/{start.isoformat()}..{end.isoformat()}"
           f"?base=USD&symbols=EUR,CNY")
    payload = http_get_json(url)
    rates = payload.get("rates", {}) if isinstance(payload, dict) else {}
    if not rates:
        failures.append({"指标": "EURUSD/USDCNY", "原因": payload.get("__error", "frankfurter 无数据")})
        return out
    keys = sorted(rates.keys())
    for disp, meaning, key in [
        ("欧元USD", "美元周期代理（DXY 中权重 57.6%，欧美错位的直接读数）", "EUR"),
        ("美元兑人民币", "人民币直接读数：数值下行=人民币升值", "CNY"),
    ]:
        series = [rates[k][key] for k in keys if key in rates[k]]
        if len(series) < 5:
            continue
        last, first = series[-1], series[0]
        out.append({
            "指标": disp, "框架含义": meaning, "代码": f"USD/{key}", "数据源": "frankfurter",
            "last": last, "change_pct": (last / first - 1) * 100,
            "high": max(series), "low": min(series),
            "口径": f"{days_back}天", "读数日期": keys[-1],
        })
    return out


# ----------------------------------------------------------------------------
# 源 3：jsDelivr currency-api —— BTC/ETH 现值 + 同比 + 月度采样区间
# ----------------------------------------------------------------------------

def fetch_jsdelivr_crypto(days_back: int, failures):
    out = []
    today = date.today()

    def usd_snapshot(d: date):
        url = (f"https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@"
               f"{d.isoformat() if d != today else 'latest'}/v1/currencies/usd.min.json")
        p = http_get_json(url)
        return p.get("usd") if isinstance(p, dict) and "usd" in p else None

    cur = usd_snapshot(today)
    if not cur:
        failures.append({"指标": "BTC/ETH", "原因": "jsDelivr 现值无数据"})
        return out
    old = usd_snapshot(today - timedelta(days=days_back))
    # 月度采样近似区间
    samples = {"btc": [], "eth": []}
    for k in range(13):
        d = today - timedelta(days=round(k * days_back / 12))
        snap = cur if d == today else usd_snapshot(d)
        if snap:
            for c in samples:
                if snap.get(c):
                    samples[c].append(1.0 / snap[c])

    for disp, meaning, key in [
        ("比特币", "现行货币体系外的另类资产，衡量投机风险偏好", "btc"),
        ("以太坊", "风险资产贝塔，衡量加密内部活跃度", "eth"),
    ]:
        try:
            last = 1.0 / cur[key]
            chg = None
            if old and old.get(key):
                chg = (last - 1.0 / old[key]) / (1.0 / old[key]) * 100
            hi = max(samples[key]) if samples[key] else None
            lo = min(samples[key]) if samples[key] else None
            out.append({"指标": disp, "框架含义": meaning,
                        "代码": key.upper() + "-USD", "数据源": "jsdelivr",
                        "last": last, "change_pct": chg, "high": hi, "low": lo,
                        "口径": f"{days_back}天(月采)", "读数日期": today.isoformat()})
        except Exception as exc:  # noqa: BLE001
            failures.append({"指标": disp, "原因": str(exc)[:120]})
    return out


# ----------------------------------------------------------------------------
# 源 4：alternative.me —— 加密恐惧贪婪
# ----------------------------------------------------------------------------

def fetch_fng():
    p = http_get_json("https://api.alternative.me/fng/?limit=1")
    if isinstance(p, dict):
        data = p.get("data")
        if isinstance(data, list) and data:
            d = data[0]
            return {"value": d.get("value"),
                    "value_classification": d.get("value_classification")}
    return None


# ----------------------------------------------------------------------------
# 缓存兜底
# ----------------------------------------------------------------------------

def load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(result):
    try:
        r = dict(result)
        r["_saved_at"] = datetime.now(timezone.utc).isoformat()
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# 汇总与渲染
# ----------------------------------------------------------------------------

def collect(days_back: int) -> dict:
    failures = []
    rows = []
    for name, meaning, code, kind in TENCENT_INDICATORS:
        r = fetch_tencent(name, meaning, code, kind, failures)
        if r:
            rows.append(r)
    rows += fetch_frankfurter(days_back, failures)
    rows += fetch_jsdelivr_crypto(days_back, failures)
    return {
        "取数日期": date.today().isoformat(),
        "统计区间": f"{days_back}天",
        "指标读数": rows,
        "加密恐惧贪婪": fetch_fng(),
        "取数失败项": failures,
    }


def fmt_money(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if f >= 1000:
        return f"{f:,.0f}"
    if f >= 1:
        return f"{f:,.2f}"
    return f"{f:.6f}"


def fmt_pct(v):
    try:
        return f"{float(v):+.1f}%"
    except (TypeError, ValueError):
        return "—"


def render_markdown(result, from_cache=False, cache_age=None):
    lines = [f"# 周期定位取数 · {result['取数日期']}（区间口径见各行列）", ""]
    if from_cache:
        warn = (f"> ⚠️ **在线源全部失败，以下为本地缓存读数（距今约 {cache_age:.0f} 天）。**"
                if cache_age is not None else
                "> ⚠️ **在线源全部失败，以下为本地缓存读数。**")
        if cache_age is not None and cache_age > STALE_DAYS:
            warn += f" **超过 {STALE_DAYS} 天，属旧值，回答中只能标注为旧值引用，不得当现值。**"
        lines += [warn, ""]

    if not result["指标读数"]:
        lines += ["> **本次未取到任何数据**，请降级为定性推理，并明确告知用户此处无实时数据。", ""]
    else:
        lines += ["| 指标 | 现值 | 区间涨跌 | 区间高 | 区间低 | 区间口径 | 数据源 |",
                  "|---|---|---|---|---|---|---|"]
        for r in result["指标读数"]:
            lines.append(
                f"| {r['指标']} | {fmt_money(r['last'])} | {fmt_pct(r['change_pct'])} "
                f"| {fmt_money(r['high'])} | {fmt_money(r['low'])} | {r['口径']} | {r['数据源']} |")
        lines.append("")

    fg = result.get("加密恐惧贪婪")
    if fg and fg.get("value"):
        lines.append(f"**加密恐惧贪婪指数**：{fg['value']} {fg.get('value_classification') or ''}")
        lines.append("")

    if result["取数失败项"]:
        lines += ["## 取数失败项（须如实告知用户，不得编造）", ""]
        for f in result["取数失败项"]:
            lines.append(f"- {f['指标']}：{f['原因']}")
        lines.append("")

    lines += ["---", "",
              "> 上表只是**客观读数**，不构成周期判断。请依据 `references/01-framework.md`、",
              "> `references/02-asset-map.md` 与 `references/03-protocol.md` 完成各层周期定位；",
              "> 数据缺失时降级为定性推理并说明；**读数超 7 天必须标注为旧值**。"]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="周金涛框架的周期定位取数器（v2 免费源）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--time-range", default="365d", choices=["30d", "90d", "180d", "365d"])
    args = ap.parse_args()

    result = collect(int(args.time_range.rstrip("d")))

    from_cache, cache_age = False, None
    if not result["指标读数"]:
        cached = load_cache()
        if cached.get("指标读数"):
            saved = cached.pop("_saved_at", None)
            age = None
            if saved:
                try:
                    age = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(saved)).total_seconds() / 86400
                except Exception:
                    age = None
            result, cache_age, from_cache = cached, age, True
    elif result["指标读数"]:
        save_cache(result)

    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json
          else render_markdown(result, from_cache, cache_age))
    return 0 if result["指标读数"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
