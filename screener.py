#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wbagger-screener : J-Quants API v2 ベースの初動スクリーニング
- 認証: APIキー方式（x-api-key ヘッダ。ダッシュボードで発行）
- 対象市場: 東証グロース＋スタンダード（criteria.yaml の markets で変更可）
- 前営業日終値ベースで初動候補を抽出
- 各候補のチャート（MA5/25/75・MACD・RCI9/26）をページに埋め込み（Chart.js描画）
- docs/data/latest.json と docs/index.html を生成

J-Quants v2 列名: bars/daily(C,O,H,L,UL,Vo,AdjC,AdjVo) / master(CoName,MktNm) /
  fins/summary(CurPerType,Sales,OP,NP,Eq,EqAR,CFO,FOP,ShOutFY,TrShFY)
投資助言ではない。最終判断は自己責任。
"""

import os
import sys
import json
import time
import datetime as dt
from typing import Any, Dict, List, Optional

import requests

try:
    import yaml
except ImportError:
    yaml = None

API_BASE = "https://api.jquants.com/v2"
JST = dt.timezone(dt.timedelta(hours=9))
CHART_POINTS = 120  # チャート表示日数

DEFAULT_CRITERIA = {
    "markets": ["グロース", "スタンダード"],
    "market_cap_oku_min": 10,
    "market_cap_oku_max": 300,
    "change_pct_min": 5.0,
    "volume_spike_min": 3.0,
    "op_margin_min": 0.13,
    "equity_ratio_min": 40.0,
    "roe_min": 8.0,
    "ma_avg_window": 20,
    "taboo_equity_ratio_max": 30.0,
    "margin_long_k_max": 1000,
}


def load_criteria() -> Dict[str, Any]:
    path = os.path.join(os.path.dirname(__file__), "criteria.yaml")
    crit = dict(DEFAULT_CRITERIA)
    if yaml and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                crit.update(yaml.safe_load(f) or {})
        except Exception as e:
            print(f"[warn] criteria.yaml 読み込み失敗: {e}")
    # 後方互換: market_name 単体指定にも対応
    if "market_name" in crit and "markets" not in crit:
        crit["markets"] = [crit["market_name"]]
    if isinstance(crit.get("markets"), str):
        crit["markets"] = [crit["markets"]]
    return crit


# ----------------------------------------------------------------------
# J-Quants v2 クライアント（APIキー方式）
# ----------------------------------------------------------------------
class JQuants:
    def __init__(self, api_key: str, min_interval: float = 1.05):
        self.session = requests.Session()
        self.api_key = api_key
        self.min_interval = min_interval
        self._last = 0.0

    def _throttle(self):
        gap = time.time() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.time()

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        params = dict(params or {})
        headers = {"x-api-key": self.api_key}
        out: List[Dict[str, Any]] = []
        for _ in range(200):
            self._throttle()
            for attempt in range(4):
                resp = self.session.get(f"{API_BASE}{path}", params=params,
                                        headers=headers, timeout=60)
                if resp.status_code == 200:
                    break
                if resp.status_code in (429, 500, 502, 503):
                    time.sleep(2 * (attempt + 1))
                    continue
                resp.raise_for_status()
            else:
                resp.raise_for_status()
            body = resp.json()
            out.extend(body.get("data", []) or [])
            pk = body.get("pagination_key")
            if not pk:
                break
            params["pagination_key"] = pk
        return out


# ----------------------------------------------------------------------
# 指標
# ----------------------------------------------------------------------
def fnum(x: Any) -> Optional[float]:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def sma(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def sma_series(values: List[float], n: int) -> List[Optional[float]]:
    res: List[Optional[float]] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            res[i] = s / n
    return res


def ema_series(values: List[float], n: int) -> List[Optional[float]]:
    res: List[Optional[float]] = [None] * len(values)
    if len(values) < n:
        return res
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    res[n - 1] = e
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        res[i] = e
    return res


def macd_series(closes: List[float]):
    e12, e26 = ema_series(closes, 12), ema_series(closes, 26)
    macd = [(a - b) if (a is not None and b is not None) else None
            for a, b in zip(e12, e26)]
    sig: List[Optional[float]] = [None] * len(macd)
    s = next((i for i, m in enumerate(macd) if m is not None), None)
    if s is not None:
        es = ema_series([m for m in macd[s:]], 9)
        for i, v in enumerate(es):
            sig[s + i] = v
    hist = [(m - g) if (m is not None and g is not None) else None
            for m, g in zip(macd, sig)]
    return macd, sig, hist


def rci_series(closes: List[float], n: int) -> List[Optional[float]]:
    res: List[Optional[float]] = [None] * len(closes)
    denom = n * (n * n - 1)
    for i in range(n - 1, len(closes)):
        w = closes[i - n + 1:i + 1]               # 古い→新しい
        order = sorted(range(n), key=lambda k: w[k], reverse=True)
        price_rank = [0] * n
        for pos, idx in enumerate(order):
            price_rank[idx] = pos + 1               # 高い=1
        sumd2 = 0.0
        for j in range(n):
            date_rank = n - j                       # 新しい=1
            d = date_rank - price_rank[j]
            sumd2 += d * d
        res[i] = round((1 - 6 * sumd2 / denom) * 100, 1)
    return res


def disp_code(code: str) -> str:
    if len(code) == 5 and code.endswith("0"):
        return code[:4]
    return code


def r2(v, nd=1):
    return None if v is None else round(v, nd)


# ----------------------------------------------------------------------
# データ取得
# ----------------------------------------------------------------------
def bars_by_date(jq: JQuants, date_str: str) -> List[Dict[str, Any]]:
    return jq.get("/equities/bars/daily", {"date": date_str})


def latest_trading_date(jq: JQuants, max_back: int = 8) -> Optional[str]:
    # 当日(夕方の更新後)から遡って、最新の取引日を探す
    today = dt.datetime.now(JST).date()
    for i in range(0, max_back + 2):
        ds = (today - dt.timedelta(days=i)).strftime("%Y-%m-%d")
        if bars_by_date(jq, ds):
            return ds
    return None


def prev_trading_date(jq: JQuants, base: str) -> Optional[str]:
    b = dt.datetime.strptime(base, "%Y-%m-%d").date()
    for i in range(1, 9):
        ds = (b - dt.timedelta(days=i)).strftime("%Y-%m-%d")
        if bars_by_date(jq, ds):
            return ds
    return None


def market_universe(jq: JQuants, target: str, markets: List[str]):
    info = jq.get("/equities/master", {"date": target})
    if not info:
        info = jq.get("/equities/master")
    codes, names, mkt = set(), {}, {}
    for r in info:
        names[r["Code"]] = r.get("CoName", "")
        mn = str(r.get("MktNm", ""))
        if any(m in mn for m in markets):
            codes.add(r["Code"])
            mkt[r["Code"]] = mn
    print(f"[ok] 対象市場 {markets} 銘柄数: {len(codes)}")
    return codes, names, mkt


def build_shortlist(jq: JQuants, target: str, prev: str, uni_codes: set,
                    crit: Dict[str, Any]) -> List[Dict[str, Any]]:
    cur = {r["Code"]: r for r in bars_by_date(jq, target)}
    prv = {r["Code"]: r for r in bars_by_date(jq, prev)}
    shortlist = []
    for code, row in cur.items():
        if uni_codes and code not in uni_codes:
            continue
        close = fnum(row.get("C"))
        pclose = fnum(prv.get(code, {}).get("C"))
        if close is None or pclose in (None, 0):
            continue
        change = (close - pclose) / pclose * 100
        stop_high = str(row.get("UL")) == "1"
        if change >= crit["change_pct_min"] or stop_high:
            shortlist.append({"code": code, "close": close,
                              "change_pct": round(change, 2),
                              "stop_high": stop_high, "volume": fnum(row.get("Vo"))})
    print(f"[ok] 一次候補(値上がり/S高): {len(shortlist)}件")
    return shortlist


def fy_rows(stmts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    fy = [s for s in stmts if str(s.get("CurPerType", "")).upper() == "FY"]
    fy.sort(key=lambda s: s.get("DiscDate", ""))
    return fy


def build_series(dates, opens, highs, lows, closes, npoints=CHART_POINTS) -> List[Dict[str, Any]]:
    ma5, ma25, ma75 = sma_series(closes, 5), sma_series(closes, 25), sma_series(closes, 75)
    macd, sig, hist = macd_series(closes)
    rci9, rci26 = rci_series(closes, 9), rci_series(closes, 26)
    n = len(closes)
    start = max(0, n - npoints)
    out = []
    for i in range(start, n):
        dlabel = dates[i][5:].replace("-", "/") if dates[i] else ""
        out.append({"d": dlabel,
                    "o": r2(opens[i], 1), "h": r2(highs[i], 1),
                    "l": r2(lows[i], 1), "c": r2(closes[i], 1),
                    "ma5": r2(ma5[i], 1), "ma25": r2(ma25[i], 1), "ma75": r2(ma75[i], 1),
                    "macd": r2(macd[i], 2), "sig": r2(sig[i], 2), "hist": r2(hist[i], 2),
                    "rci9": r2(rci9[i], 1), "rci26": r2(rci26[i], 1)})
    return out


def resample_weekly(dates, opens, highs, lows, closes):
    """日足を週足OHLCに再集計（ISO週）。"""
    wd, wo, wh, wl, wc = [], [], [], [], []
    cur = None
    bo = bh = bl = bc = bdate = None
    for i, ds in enumerate(dates):
        if not ds:
            continue
        y, wk, _ = dt.date.fromisoformat(ds).isocalendar()
        key = (y, wk)
        if key != cur:
            if cur is not None:
                wd.append(bdate); wo.append(bo); wh.append(bh); wl.append(bl); wc.append(bc)
            cur = key
            bo, bh, bl, bc, bdate = opens[i], highs[i], lows[i], closes[i], ds
        else:
            if highs[i] is not None:
                bh = highs[i] if bh is None else max(bh, highs[i])
            if lows[i] is not None:
                bl = lows[i] if bl is None else min(bl, lows[i])
            if closes[i] is not None:
                bc = closes[i]
            if bo is None:
                bo = opens[i]
            bdate = ds
    if cur is not None:
        wd.append(bdate); wo.append(bo); wh.append(bh); wl.append(bl); wc.append(bc)
    return wd, wo, wh, wl, wc


def analyze_candidate(jq: JQuants, item: Dict[str, Any], names: Dict[str, str],
                      mkt: Dict[str, str], crit: Dict[str, Any]) -> Dict[str, Any]:
    code = item["code"]
    rec = {
        "code": disp_code(code), "raw_code": code,
        "name": names.get(code, ""), "market": mkt.get(code, ""),
        "price": item["close"], "change_pct": item["change_pct"],
        "stop_high": item["stop_high"],
        "volume_x": None, "market_cap_oku": None,
        "ma_perfect_order": None, "macd_cross": None,
        "op_margin": None, "equity_ratio": None, "roe": None,
        "profit_trend": None, "taboo_hit": None, "taboo_reason": "",
        "margin_long_k": None, "stop_loss": None, "manual_note": "",
        "chart": [],
    }

    frm = (dt.datetime.strptime(item["date_target"], "%Y-%m-%d").date()
           - dt.timedelta(days=760)).strftime("%Y-%m-%d")
    hist = jq.get("/equities/bars/daily",
                  {"code": code, "from": frm, "to": item["date_target"]})
    hist = [h for h in hist if fnum(h.get("AdjC")) is not None]
    hist.sort(key=lambda h: h.get("Date", ""))
    dates = [h.get("Date", "") for h in hist]
    opens = [fnum(h.get("AdjO")) for h in hist]
    highs = [fnum(h.get("AdjH")) for h in hist]
    lows = [fnum(h.get("AdjL")) for h in hist]
    closes = [fnum(h.get("AdjC")) for h in hist]
    vols = [fnum(h.get("AdjVo")) or 0 for h in hist]
    if len(closes) >= 5:
        ma5, ma25 = sma(closes, 5), sma(closes, 25)
        ma75, ma200 = sma(closes, 75), sma(closes, 200)
        price = closes[-1]
        if None not in (ma5, ma25, ma75, ma200):
            rec["ma_perfect_order"] = price > ma5 > ma25 > ma75 > ma200
        rec["stop_loss"] = round(min(closes[-5:]))
        macd, sig, _ = macd_series(closes)
        if macd[-1] is not None and sig[-1] is not None:
            rec["macd_cross"] = macd[-1] > sig[-1]
        wd, wo, wh, wl, wc = resample_weekly(dates, opens, highs, lows, closes)
        rec["chart"] = {"d": build_series(dates, opens, highs, lows, closes),
                        "w": build_series(wd, wo, wh, wl, wc)}
    if len(vols) > crit["ma_avg_window"]:
        base = sum(vols[-(crit["ma_avg_window"] + 1):-1]) / crit["ma_avg_window"]
        if base > 0 and vols[-1]:
            rec["volume_x"] = round(vols[-1] / base, 1)

    stmts = jq.get("/fins/summary", {"code": code})
    fy = fy_rows(stmts)
    st = fy[-1] if fy else (stmts[-1] if stmts else None)
    if st:
        sales, op, np_ = fnum(st.get("Sales")), fnum(st.get("OP")), fnum(st.get("NP"))
        eq, eqar = fnum(st.get("Eq")), fnum(st.get("EqAR"))
        if eqar is not None:
            eqar *= 100
        f_op = fnum(st.get("FOP"))
        shares = fnum(st.get("ShOutFY"))
        treasury = fnum(st.get("TrShFY")) or 0
        if sales and op is not None:
            rec["op_margin"] = round(op / sales, 3)
        if eqar is not None:
            rec["equity_ratio"] = round(eqar, 1)
        if eq and np_ is not None and eq != 0:
            rec["roe"] = round(np_ / eq * 100, 1)
        ops = [fnum(r.get("OP")) for r in fy if fnum(r.get("OP")) is not None]
        if len(ops) >= 2:
            rec["profit_trend"] = "増益" if ops[-1] > ops[-2] else "減益/横ばい"
        elif f_op is not None and op is not None:
            rec["profit_trend"] = "増益" if f_op > op else "減益/横ばい"
        if shares:
            rec["market_cap_oku"] = round(item["close"] * (shares - treasury) / 1e8, 1)
        reasons = []
        if rec["equity_ratio"] is not None and \
                rec["equity_ratio"] <= crit["taboo_equity_ratio_max"]:
            reasons.append(f"自己資本比率{rec['equity_ratio']}%≤{crit['taboo_equity_ratio_max']}%")
        cfs = [fnum(r.get("CFO")) for r in fy if fnum(r.get("CFO")) is not None][-3:]
        if len(cfs) == 3 and all(c < 0 for c in cfs):
            reasons.append("営業CF3期連続マイナス")
        rec["taboo_hit"] = len(reasons) > 0
        rec["taboo_reason"] = " / ".join(reasons)

    try:
        mgn = jq.get("/markets/margin-interest", {"code": code})
        if mgn:
            mgn.sort(key=lambda m: m.get("Date", ""))
            last = mgn[-1]
            for key in ("LongMarginTradeVolume", "LongVo", "Long", "LongMargin", "LMgn"):
                if key in last:
                    lv = fnum(last.get(key))
                    if lv is not None:
                        rec["margin_long_k"] = round(lv / 1000, 1)
                    break
    except Exception:
        pass

    return rec


def label(rec: Dict[str, Any], crit: Dict[str, Any]) -> str:
    if rec.get("taboo_hit"):
        return "除外"
    cap = rec.get("market_cap_oku")
    if cap is not None and not (crit["market_cap_oku_min"] <= cap <= crit["market_cap_oku_max"]):
        return "除外"
    spike_ok = (rec.get("volume_x") or 0) >= crit["volume_spike_min"]
    po = rec.get("ma_perfect_order")
    if rec.get("stop_high") or (rec.get("change_pct", 0) >= crit["change_pct_min"]
                                and spike_ok and po):
        return "初動入口◎"
    if po or spike_ok:
        return "押し目待ち○"
    return "監視△"


# ----------------------------------------------------------------------
# 出力
# ----------------------------------------------------------------------
def render(target: str, records: List[Dict[str, Any]]):
    docs = os.path.join(os.path.dirname(__file__), "docs")
    os.makedirs(os.path.join(docs, "data"), exist_ok=True)
    payload = {"generated_at": dt.datetime.now(JST).isoformat(timespec="seconds"),
               "data_date": target, "candidates": records}
    with open(os.path.join(docs, "data", "latest.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    order = {"初動入口◎": 0, "押し目待ち○": 1, "監視△": 2, "除外": 3}
    records = sorted(records, key=lambda r: (order.get(r["label"], 9),
                                             -(r.get("change_pct") or 0)))
    rows = []
    for r in records:
        def cell(v, suf=""):
            return "—" if v is None else f"{v}{suf}"

        def tri(v):
            return "○" if v else ("—" if v is None else "×")
        rows.append(f"""<tr onclick="showChart('{r['raw_code']}')">
<td>{r['label']}</td><td>{r['code']}</td><td>{r['name']}</td><td>{r.get('market','')}</td>
<td class="num">¥{cell(r.get('price'))}</td>
<td class="num">{cell(r.get('change_pct'),'%')}</td>
<td class="num">{cell(r.get('volume_x'),'x')}</td>
<td>{'S高' if r.get('stop_high') else ''}</td>
<td class="num">{cell(r.get('market_cap_oku'),'億')}</td>
<td class="num">{cell(r.get('equity_ratio'),'%')}</td>
<td>{cell(r.get('profit_trend'))}</td>
<td>{tri(r.get('ma_perfect_order'))}</td>
<td>{tri(r.get('macd_cross'))}</td>
<td class="num">{cell(r.get('stop_loss'))}</td>
<td class="warn">{r.get('taboo_reason','')}</td>
</tr>""")

    chart_map = {r["raw_code"]: {"name": r["name"], "code": r["code"],
                                 "chart": r.get("chart", [])} for r in records}
    data_js = json.dumps(chart_map, ensure_ascii=False)

    html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>初動スクリーニング (wbagger-screener)</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
body{{font-family:system-ui,'Hiragino Sans',sans-serif;margin:16px;background:#0d1117;color:#e6edf3}}
h1{{font-size:18px}} .meta{{color:#8b949e;font-size:13px;margin-bottom:12px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border:1px solid #30363d;padding:5px 7px;text-align:left}}
th{{background:#161b22;position:sticky;top:0}}
tbody tr{{cursor:pointer}} tbody tr:hover{{background:#21262d}}
td.num{{text-align:right}} td.warn{{color:#f85149}}
tr.lab-0{{background:#13301f}} tr.lab-1{{background:#1b2433}} tr.lab-3{{opacity:.6}}
.note{{color:#8b949e;font-size:12px;margin-top:14px;line-height:1.6}}
#panel{{display:none;margin:16px 0;padding:12px;border:1px solid #30363d;border-radius:8px;background:#0f141b}}
#panel h2{{font-size:15px;margin:.2em 0 .6em}}
#tfbtns{{margin-bottom:8px}}
#tfbtns button{{background:#21262d;color:#e6edf3;border:1px solid #30363d;padding:4px 14px;margin-right:6px;border-radius:6px;cursor:pointer;font-size:12px}}
.cwrap{{position:relative;height:220px;margin-bottom:10px}}
.cwrap.small{{height:140px}}
.close{{float:right;color:#8b949e;cursor:pointer}}
</style></head><body>
<h1>初動スクリーニング — 東証グロース＋スタンダード</h1>
<div class="meta">データ基準日: {target} ／ 生成: {dt.datetime.now(JST).strftime('%Y-%m-%d %H:%M')} JST ／ 件数: {len(records)} ／ 行クリックでチャート表示</div>

<div id="panel">
  <span class="close" onclick="document.getElementById('panel').style.display='none'">閉じる ✕</span>
  <h2 id="ptitle"></h2>
  <div id="tfbtns"><button id="btf-d" onclick="setTf('d')">日足</button><button id="btf-w" onclick="setTf('w')">週足</button></div>
  <div class="cwrap"><canvas id="cPrice"></canvas></div>
  <div class="cwrap small"><canvas id="cMacd"></canvas></div>
  <div class="cwrap small"><canvas id="cRci"></canvas></div>
</div>

<table><thead><tr>
<th>判定</th><th>コード</th><th>銘柄</th><th>市場</th><th>株価(終値)</th><th>前日比</th><th>出来高倍</th><th>S高</th>
<th>時価総額</th><th>自己資本</th><th>増益</th><th>P.O.</th><th>MACD</th><th>逆指値目安</th><th>タブー</th>
</tr></thead><tbody>
{''.join(rows) if rows else '<tr><td colspan="15">該当なし</td></tr>'}
</tbody></table>
<div class="note">
P.O.=パーフェクトオーダー。行クリックで価格+MA(5/25/75)・MACD・RCI(9/26)を表示。「—」はデータ取得不可。<br>
材料・テーマの中身はJ-Quants非配信のため別途TDnet/EDINETで確認すること。逆指値目安は直近5日安値。<br>
本表は手法に基づく機械的抽出であり投資助言ではない。最終判断は自己責任。
</div>

<script>
const DATA = {data_js};
let charts = [];
function mk(id, cfg) {{
  const el = document.getElementById(id);
  return new Chart(el, cfg);
}}
function line(label, key, rows, color, opt) {{
  return Object.assign({{type:'line', label, data: rows.map(r=>r[key]), borderColor: color,
    borderWidth: 1.4, pointRadius: 0, tension: .15, spanGaps: true}}, opt||{{}});
}}
let curCode = null, curTf = 'd';
function updateTfBtns() {{
  ['d','w'].forEach(t=>{{ const b=document.getElementById('btf-'+t);
    if(b) b.style.background = (t===curTf ? '#1f6feb' : '#21262d'); }});
}}
function setTf(tf) {{ if(curCode) showChart(curCode, tf); }}
function showChart(code, tf) {{
  const d = DATA[code];
  curCode = code; curTf = (tf || 'd'); updateTfBtns();
  charts.forEach(c=>c.destroy()); charts = [];
  const panel = document.getElementById('panel'); panel.style.display = 'block';
  panel.scrollIntoView({{behavior:'smooth',block:'start'}});
  const series = (d && d.chart) ? d.chart[curTf] : null;
  if (!d || !series || !series.length) {{
    document.getElementById('ptitle').textContent =
      (d ? d.code + ' ' + d.name : code) + ' — チャートデータ不足（新規上場等）';
    return;
  }}
  const rows = series, labels = rows.map(r=>r.d);
  const tfName = (curTf==='w' ? '週足' : '日足');
  const lastC = rows[rows.length-1].c, lastD = rows[rows.length-1].d;
  document.getElementById('ptitle').textContent =
    d.code + ' ' + d.name + '　[' + tfName + ']　終値 ¥' + lastC + '（' + lastD + '）';
  const grid = {{color:'#222',drawTicks:false}}, tick={{color:'#8b949e',maxTicksLimit:8,font:{{size:9}}}};
  const up='#3fb950', dn='#f85149';
  const wick={{type:'bar',label:'wick',data:rows.map(r=>(r.l!=null&&r.h!=null)?[r.l,r.h]:null),
    backgroundColor:'#6e7681',barPercentage:0.12,categoryPercentage:0.9,order:3}};
  const body={{type:'bar',label:'ローソク',data:rows.map(r=>(r.o!=null&&r.c!=null)?[Math.min(r.o,r.c),Math.max(r.o,r.c)]:null),
    backgroundColor:rows.map(r=>(r.c!=null&&r.o!=null&&r.c>=r.o)?up:dn),barPercentage:0.55,categoryPercentage:0.9,order:2}};
  charts.push(mk('cPrice', {{type:'bar', data:{{labels, datasets:[wick, body,
      line('MA5','ma5',rows,'#f0a020',{{order:1}}), line('MA25','ma25',rows,'#58a6ff',{{order:1}}),
      line('MA75','ma75',rows,'#d2a8ff',{{order:1}})]}},
    options:{{responsive:true,maintainAspectRatio:false,interaction:{{intersect:false,mode:'index'}},
      plugins:{{legend:{{labels:{{color:'#8b949e',boxWidth:10,font:{{size:10}},filter:(it)=>it.text!=='wick'}}}}}},
      scales:{{x:{{grid,ticks:tick}},y:{{beginAtZero:false,grid,ticks:{{color:'#8b949e',font:{{size:9}}}}}}}}}}}}));
  charts.push(mk('cMacd', {{type:'bar', data:{{labels, datasets:[
      Object.assign({{type:'bar',label:'Hist',data:rows.map(r=>r.hist),backgroundColor:'#39506b'}}),
      line('MACD','macd',rows,'#f0a020'), line('Signal','sig',rows,'#f85149')]}},
    options:{{responsive:true,maintainAspectRatio:false,
      plugins:{{legend:{{labels:{{color:'#8b949e',boxWidth:10,font:{{size:10}}}}}}}},
      scales:{{x:{{grid,ticks:tick}},y:{{grid,ticks:{{color:'#8b949e',font:{{size:9}}}}}}}}}}}}));
  charts.push(mk('cRci', {{type:'line', data:{{labels, datasets:[
      line('RCI9','rci9',rows,'#f0a020'), line('RCI26','rci26',rows,'#58a6ff')]}},
    options:{{responsive:true,maintainAspectRatio:false,
      plugins:{{legend:{{labels:{{color:'#8b949e',boxWidth:10,font:{{size:10}}}}}}}},
      scales:{{x:{{grid,ticks:tick}},y:{{min:-100,max:100,grid,
        ticks:{{color:'#8b949e',font:{{size:9}},stepSize:50}}}}}}}}}}));
  document.getElementById('panel').scrollIntoView({{behavior:'smooth',block:'start'}});
}}
</script>
</body></html>"""
    with open(os.path.join(docs, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[ok] 出力完了: docs/index.html, docs/data/latest.json ({len(records)}件)")


def main() -> int:
    api_key = os.environ.get("JQUANTS_API_KEY")
    if not api_key:
        print("[error] 環境変数 JQUANTS_API_KEY が未設定（v2はAPIキー方式）")
        return 1
    crit = load_criteria()
    jq = JQuants(api_key)

    target = latest_trading_date(jq)
    if not target:
        print("[error] 取引日データが見つかりません(配信状況/APIキーを確認)")
        return 1
    prev = prev_trading_date(jq, target)
    if not prev:
        print("[error] 前営業日が特定できません")
        return 1
    print(f"[ok] 対象日={target} / 前日={prev}")

    uni_codes, names, mkt = market_universe(jq, target, crit["markets"])
    shortlist = build_shortlist(jq, target, prev, uni_codes, crit)
    for it in shortlist:
        it["date_target"] = target

    records = []
    for i, item in enumerate(shortlist, 1):
        try:
            rec = analyze_candidate(jq, item, names, mkt, crit)
            rec["label"] = label(rec, crit)
            records.append(rec)
            print(f"  [{i}/{len(shortlist)}] {rec['code']} {rec['name']} ({rec['market']}) -> {rec['label']}")
        except Exception as e:
            print(f"  [warn] {item['code']} 解析失敗: {e}")

    render(target, records)
    return 0


if __name__ == "__main__":
    sys.exit(main())
