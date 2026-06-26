#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wbagger-screener : J-Quants API v2 ベースの初動スクリーニング
- 認証: APIキー方式（x-api-key ヘッダ。ダッシュボードで発行）
- 前営業日終値ベースで東証グロースの初動候補を抽出
- docs/data/latest.json と docs/index.html を生成
本処理は GitHub Actions 上で動かす想定。ローカル実行も可。

J-Quants v2 仕様（要点）:
- Base: https://api.jquants.com/v2
- レスポンスは {"data":[...], "pagination_key":...}
- 株価 /equities/bars/daily : Date,Code,O,H,L,C,UL,LL,Vo,Va,AdjC,AdjVo ...
- 銘柄 /equities/master      : Code,CoName,MktNm(例 "グロース") ...
- 財務 /fins/summary         : CurPerType,Sales,OP,NP,TA,Eq,EqAR(小数),CFO,FOP,ShOutFY,TrShFY ...
- 信用 /markets/margin-interest（列名未確認のため防御的に取得）

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

DEFAULT_CRITERIA = {
    "market_name": "グロース",
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
    return crit


# ----------------------------------------------------------------------
# J-Quants v2 クライアント（APIキー方式）
# ----------------------------------------------------------------------
class JQuants:
    def __init__(self, api_key: str, min_interval: float = 1.05):
        self.session = requests.Session()
        self.api_key = api_key
        self.min_interval = min_interval   # Light=60req/min → 約1.05秒/req
        self._last = 0.0

    def _throttle(self):
        gap = time.time() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.time()

    def get(self, path: str, params: Optional[Dict[str, Any]] = None,
            data_key: str = "data") -> List[Dict[str, Any]]:
        params = dict(params or {})
        headers = {"x-api-key": self.api_key}
        out: List[Dict[str, Any]] = []
        for _ in range(200):
            self._throttle()
            resp = None
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
            out.extend(body.get(data_key, []) or [])
            pk = body.get("pagination_key")
            if not pk:
                break
            params["pagination_key"] = pk
        return out


# ----------------------------------------------------------------------
# ユーティリティ / 指標
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


def ema(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def macd_cross_up(closes: List[float]) -> Optional[bool]:
    if len(closes) < 35:
        return None
    macd_line = []
    for i in range(26, len(closes) + 1):
        e12, e26 = ema(closes[:i], 12), ema(closes[:i], 26)
        if e12 is None or e26 is None:
            continue
        macd_line.append(e12 - e26)
    if len(macd_line) < 10:
        return None
    signal = ema(macd_line, 9)
    if signal is None:
        return None
    return macd_line[-1] > signal


def disp_code(code: str) -> str:
    if len(code) == 5 and code.endswith("0"):
        return code[:4]
    return code


# ----------------------------------------------------------------------
# データ取得
# ----------------------------------------------------------------------
def bars_by_date(jq: JQuants, date_str: str) -> List[Dict[str, Any]]:
    return jq.get("/equities/bars/daily", {"date": date_str})


def latest_trading_date(jq: JQuants, max_back: int = 8) -> Optional[str]:
    today = dt.datetime.now(JST).date()
    for i in range(1, max_back + 2):
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


def growth_universe(jq: JQuants, target: str, market_name: str):
    info = jq.get("/equities/master", {"date": target})
    if not info:
        info = jq.get("/equities/master")
    codes, names = set(), {}
    for r in info:
        names[r["Code"]] = r.get("CoName", "")
        if market_name in str(r.get("MktNm", "")):
            codes.add(r["Code"])
    print(f"[ok] 東証{market_name} 銘柄数: {len(codes)}")
    return codes, names


def build_shortlist(jq: JQuants, target: str, prev: str, growth_codes: set,
                    crit: Dict[str, Any]) -> List[Dict[str, Any]]:
    cur = {r["Code"]: r for r in bars_by_date(jq, target)}
    prv = {r["Code"]: r for r in bars_by_date(jq, prev)}
    shortlist = []
    for code, row in cur.items():
        if growth_codes and code not in growth_codes:
            continue
        close = fnum(row.get("C"))
        pclose = fnum(prv.get(code, {}).get("C"))
        if close is None or pclose in (None, 0):
            continue
        change = (close - pclose) / pclose * 100
        stop_high = str(row.get("UL")) == "1"
        if change >= crit["change_pct_min"] or stop_high:
            shortlist.append({
                "code": code, "close": close,
                "change_pct": round(change, 2),
                "stop_high": stop_high, "volume": fnum(row.get("Vo")),
            })
    print(f"[ok] 一次候補(グロース・値上がり/S高): {len(shortlist)}件")
    return shortlist


def fy_rows(stmts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    fy = [s for s in stmts if str(s.get("CurPerType", "")).upper() == "FY"]
    fy.sort(key=lambda s: s.get("DiscDate", ""))
    return fy


def analyze_candidate(jq: JQuants, item: Dict[str, Any],
                      names: Dict[str, str], crit: Dict[str, Any]) -> Dict[str, Any]:
    code = item["code"]
    rec = {
        "code": disp_code(code), "raw_code": code,
        "name": names.get(code, ""),
        "price": item["close"], "change_pct": item["change_pct"],
        "stop_high": item["stop_high"],
        "volume_x": None, "market_cap_oku": None,
        "ma_perfect_order": None, "macd_cross": None,
        "op_margin": None, "equity_ratio": None, "roe": None,
        "profit_trend": None, "taboo_hit": None, "taboo_reason": "",
        "margin_long_k": None, "stop_loss": None, "manual_note": "",
    }

    # --- 株価履歴(直近約1.5年): MA / MACD / 出来高倍率 ---
    frm = (dt.datetime.strptime(item.get("date_target", dt.datetime.now(JST)
            .strftime("%Y-%m-%d")), "%Y-%m-%d").date()
           - dt.timedelta(days=400)).strftime("%Y-%m-%d")
    hist = jq.get("/equities/bars/daily", {"code": code, "from": frm,
                  "to": item.get("date_target")})
    hist = [h for h in hist if fnum(h.get("C")) is not None]
    hist.sort(key=lambda h: h.get("Date", ""))
    closes = [fnum(h.get("C")) for h in hist]
    vols = [fnum(h.get("Vo")) or 0 for h in hist]
    if len(closes) >= 5:
        ma5, ma25 = sma(closes, 5), sma(closes, 25)
        ma75, ma200 = sma(closes, 75), sma(closes, 200)
        price = closes[-1]
        if None not in (ma5, ma25, ma75, ma200):
            rec["ma_perfect_order"] = price > ma5 > ma25 > ma75 > ma200
        rec["stop_loss"] = round(min(closes[-5:]))
        rec["macd_cross"] = macd_cross_up(closes)
    if len(vols) > crit["ma_avg_window"]:
        base = sum(vols[-(crit["ma_avg_window"] + 1):-1]) / crit["ma_avg_window"]
        if base > 0 and vols[-1]:
            rec["volume_x"] = round(vols[-1] / base, 1)

    # --- 財務サマリ ---
    stmts = jq.get("/fins/summary", {"code": code})
    fy = fy_rows(stmts)
    st = fy[-1] if fy else (stmts[-1] if stmts else None)
    if st:
        sales = fnum(st.get("Sales"))
        op = fnum(st.get("OP"))
        np_ = fnum(st.get("NP"))
        eq = fnum(st.get("Eq"))
        eqar = fnum(st.get("EqAR"))
        if eqar is not None:
            eqar *= 100  # v2 EqAR は小数比率
        f_op = fnum(st.get("FOP"))
        shares = fnum(st.get("ShOutFY"))
        treasury = fnum(st.get("TrShFY")) or 0

        if sales and op is not None:
            rec["op_margin"] = round(op / sales, 3)
        if eqar is not None:
            rec["equity_ratio"] = round(eqar, 1)
        if eq and np_ is not None and eq != 0:
            rec["roe"] = round(np_ / eq * 100, 1)
        # 増益: 直近FYのOPが前FYより増加、無ければ予想FOP>OP
        ops = [fnum(r.get("OP")) for r in fy if fnum(r.get("OP")) is not None]
        if len(ops) >= 2:
            rec["profit_trend"] = "増益" if ops[-1] > ops[-2] else "減益/横ばい"
        elif f_op is not None and op is not None:
            rec["profit_trend"] = "増益" if f_op > op else "減益/横ばい"
        if shares:
            rec["market_cap_oku"] = round(item["close"] * (shares - treasury) / 1e8, 1)

        # タブー
        reasons = []
        if rec["equity_ratio"] is not None and \
                rec["equity_ratio"] <= crit["taboo_equity_ratio_max"]:
            reasons.append(f"自己資本比率{rec['equity_ratio']}%≤{crit['taboo_equity_ratio_max']}%")
        cfs = [fnum(r.get("CFO")) for r in fy if fnum(r.get("CFO")) is not None][-3:]
        if len(cfs) == 3 and all(c < 0 for c in cfs):
            reasons.append("営業CF3期連続マイナス")
        rec["taboo_hit"] = len(reasons) > 0
        rec["taboo_reason"] = " / ".join(reasons)

    # --- 信用残(列名未確認のため防御的) ---
    try:
        mgn = jq.get("/markets/margin-interest", {"code": code})
        if mgn:
            mgn.sort(key=lambda m: m.get("Date", ""))
            last = mgn[-1]
            longv = None
            for key in ("LongMarginTradeVolume", "LongVo", "Long",
                        "LongMargin", "LMgn"):
                if key in last:
                    longv = fnum(last.get(key))
                    break
            if longv is not None:
                rec["margin_long_k"] = round(longv / 1000, 1)
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
    payload = {
        "generated_at": dt.datetime.now(JST).isoformat(timespec="seconds"),
        "data_date": target,
        "candidates": records,
    }
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
        rows.append(f"""<tr class="lab-{order.get(r['label'],9)}">
<td>{r['label']}</td><td>{r['code']}</td><td>{r['name']}</td>
<td class="num">{cell(r.get('change_pct'),'%')}</td>
<td class="num">{cell(r.get('volume_x'),'x')}</td>
<td>{'S高' if r.get('stop_high') else ''}</td>
<td class="num">{cell(r.get('market_cap_oku'),'億')}</td>
<td class="num">{cell(r.get('equity_ratio'),'%')}</td>
<td>{cell(r.get('profit_trend'))}</td>
<td>{tri(r.get('ma_perfect_order'))}</td>
<td>{tri(r.get('macd_cross'))}</td>
<td class="num">{cell(r.get('margin_long_k'),'千')}</td>
<td class="num">{cell(r.get('stop_loss'))}</td>
<td class="warn">{r.get('taboo_reason','')}</td>
</tr>""")
    html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>初動スクリーニング (wbagger-screener)</title>
<style>
body{{font-family:system-ui,'Hiragino Sans',sans-serif;margin:16px;background:#0d1117;color:#e6edf3}}
h1{{font-size:18px}} .meta{{color:#8b949e;font-size:13px;margin-bottom:12px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border:1px solid #30363d;padding:5px 7px;text-align:left}}
th{{background:#161b22;position:sticky;top:0}}
td.num{{text-align:right}} td.warn{{color:#f85149}}
tr.lab-0{{background:#13301f}} tr.lab-1{{background:#1b2433}} tr.lab-3{{opacity:.55}}
.note{{color:#8b949e;font-size:12px;margin-top:14px;line-height:1.6}}
</style></head><body>
<h1>初動スクリーニング — 東証グロース</h1>
<div class="meta">データ基準日: {target} ／ 生成: {dt.datetime.now(JST).strftime('%Y-%m-%d %H:%M')} JST ／ 件数: {len(records)}</div>
<table><thead><tr>
<th>判定</th><th>コード</th><th>銘柄</th><th>前日比</th><th>出来高倍</th><th>S高</th>
<th>時価総額</th><th>自己資本</th><th>増益</th><th>P.O.</th><th>MACD</th><th>信用買残</th><th>逆指値目安</th><th>タブー</th>
</tr></thead><tbody>
{''.join(rows) if rows else '<tr><td colspan="14">該当なし</td></tr>'}
</tbody></table>
<div class="note">
P.O.=パーフェクトオーダー。「—」はデータ取得不可。材料・テーマの中身はJ-Quants非配信のため別途TDnet/EDINETで確認すること。<br>
本表は手法に基づく機械的抽出であり投資助言ではない。最終判断は自己責任。逆指値目安は直近5日安値。
</div></body></html>"""
    with open(os.path.join(docs, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[ok] 出力完了: docs/index.html, docs/data/latest.json ({len(records)}件)")


# ----------------------------------------------------------------------
# メイン
# ----------------------------------------------------------------------
def main() -> int:
    api_key = os.environ.get("JQUANTS_API_KEY")
    if not api_key:
        print("[error] 環境変数 JQUANTS_API_KEY が未設定（J-Quants v2 はAPIキー方式）")
        return 1
    crit = load_criteria()
    jq = JQuants(api_key)

    target = latest_trading_date(jq)
    if not target:
        print("[error] 取引日データが見つかりません(プランの配信状況/APIキーを確認)")
        return 1
    prev = prev_trading_date(jq, target)
    if not prev:
        print("[error] 前営業日が特定できません")
        return 1
    print(f"[ok] 対象日={target} / 前日={prev}")

    growth_codes, names = growth_universe(jq, target, crit["market_name"])
    shortlist = build_shortlist(jq, target, prev, growth_codes, crit)
    for it in shortlist:
        it["date_target"] = target

    records = []
    for i, item in enumerate(shortlist, 1):
        try:
            rec = analyze_candidate(jq, item, names, crit)
            rec["label"] = label(rec, crit)
            records.append(rec)
            print(f"  [{i}/{len(shortlist)}] {rec['code']} {rec['name']} -> {rec['label']}")
        except Exception as e:
            print(f"  [warn] {item['code']} 解析失敗: {e}")

    render(target, records)
    return 0


if __name__ == "__main__":
    sys.exit(main())
