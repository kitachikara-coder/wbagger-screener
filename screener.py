#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wbagger-screener : J-Quants ベースの初動スクリーニング
- 認証 (mailaddress/password -> refreshToken -> idToken)
- 前営業日終値ベースで東証グロースの初動候補を抽出
- docs/data/latest.json と docs/index.html を生成
本処理は GitHub Actions 上で動かす想定。ローカル実行も可。

注意:
- J-Quants の正確なフィールド名/挙動は公式 API リファレンスで要確認。
  本コードは v1 想定で防御的に実装(欠損は None 扱いで停止しない)。
- 投資助言ではない。最終判断は自己責任。
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

API_BASE = "https://api.jquants.com/v1"
JST = dt.timezone(dt.timedelta(hours=9))

# ----------------------------------------------------------------------
# 設定読み込み
# ----------------------------------------------------------------------
DEFAULT_CRITERIA = {
    "market_name": "グロース",          # listed/info の MarketCodeName で絞り込み
    "market_cap_oku_min": 10,           # 時価総額 下限(億円)
    "market_cap_oku_max": 300,          # 時価総額 上限(億円)
    "change_pct_min": 5.0,              # 一次絞り込み: 前日比 +%以上 を候補に
    "volume_spike_min": 3.0,            # 出来高 = 過去20日平均の何倍以上
    "op_margin_min": 0.13,              # 営業利益率 下限
    "equity_ratio_min": 40.0,           # 自己資本比率 下限(%)
    "roe_min": 8.0,                     # ROE 下限(%)
    "ma_avg_window": 20,                # 出来高平均の期間
    "taboo_equity_ratio_max": 30.0,     # タブー: 自己資本比率 <= これ
    "margin_long_k_max": 1000,          # 信用買残 これ(千株)以下を「少」とみなす目安
}


def load_criteria() -> Dict[str, Any]:
    path = os.path.join(os.path.dirname(__file__), "criteria.yaml")
    crit = dict(DEFAULT_CRITERIA)
    if yaml and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user = yaml.safe_load(f) or {}
            crit.update(user)
        except Exception as e:
            print(f"[warn] criteria.yaml 読み込み失敗: {e}")
    return crit


# ----------------------------------------------------------------------
# J-Quants クライアント
# ----------------------------------------------------------------------
class JQuants:
    def __init__(self, mail: str, password: str):
        self.session = requests.Session()
        self.id_token = self._auth(mail, password)

    def _auth(self, mail: str, password: str) -> str:
        # 1) refreshToken
        r = self.session.post(
            f"{API_BASE}/token/auth_user",
            data=json.dumps({"mailaddress": mail, "password": password}),
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        refresh = r.json().get("refreshToken")
        if not refresh:
            raise RuntimeError("refreshToken を取得できません(認証情報を確認)")
        # 2) idToken
        r2 = self.session.post(
            f"{API_BASE}/token/auth_refresh",
            params={"refreshtoken": refresh},
            timeout=30,
        )
        r2.raise_for_status()
        idt = r2.json().get("idToken")
        if not idt:
            raise RuntimeError("idToken を取得できません")
        print("[ok] J-Quants 認証成功")
        return idt

    def get(self, path: str, params: Optional[Dict[str, Any]] = None,
            data_key: Optional[str] = None) -> List[Dict[str, Any]]:
        """pagination_key を辿って全件取得。data_key のリストを返す。"""
        params = dict(params or {})
        headers = {"Authorization": f"Bearer {self.id_token}"}
        out: List[Dict[str, Any]] = []
        for _ in range(50):  # 安全のための上限
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
            if data_key is None:
                # data_key 自動判定: pagination_key 以外の最初のリスト
                data_key = next((k for k, v in body.items()
                                 if isinstance(v, list)), None)
            if data_key:
                out.extend(body.get(data_key, []))
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
    """MACD がシグナルを上抜けている状態か(好転)。"""
    if len(closes) < 35:
        return None
    macd_line = []
    for i in range(26, len(closes) + 1):
        sub = closes[:i]
        e12, e26 = ema(sub, 12), ema(sub, 26)
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
    """J-Quants の5桁内部コードを表示用に。"""
    if len(code) == 5 and code.endswith("0"):
        return code[:4]
    return code


# ----------------------------------------------------------------------
# データ取得段階
# ----------------------------------------------------------------------
def latest_trading_date(jq: JQuants, max_back: int = 7) -> Optional[str]:
    """前営業日から遡って、daily_quotes が返る最新日を探す。"""
    today = dt.datetime.now(JST).date()
    for i in range(1, max_back + 2):
        d = today - dt.timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        rows = jq.get("/prices/daily_quotes", {"date": ds}, "daily_quotes")
        if rows:
            return ds
    return None


def prev_trading_date(jq: JQuants, base: str) -> Optional[str]:
    b = dt.datetime.strptime(base, "%Y-%m-%d").date()
    for i in range(1, 9):
        d = b - dt.timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        rows = jq.get("/prices/daily_quotes", {"date": ds}, "daily_quotes")
        if rows:
            return ds
    return None


def build_universe(jq: JQuants, target: str, prev: str,
                   growth_codes: set) -> List[Dict[str, Any]]:
    """対象日の全銘柄から、東証グロースかつ値上がり/ S高 の一次候補を返す。"""
    cur = {r["Code"]: r for r in jq.get("/prices/daily_quotes",
                                        {"date": target}, "daily_quotes")}
    prv = {r["Code"]: r for r in jq.get("/prices/daily_quotes",
                                        {"date": prev}, "daily_quotes")}
    crit = load_criteria()
    shortlist = []
    for code, row in cur.items():
        if growth_codes and code not in growth_codes:
            continue
        close = fnum(row.get("Close"))
        pclose = fnum(prv.get(code, {}).get("Close"))
        if close is None or pclose is None or pclose == 0:
            continue
        change = (close - pclose) / pclose * 100
        stop_high = str(row.get("UpperLimit")) == "1"
        if change >= crit["change_pct_min"] or stop_high:
            shortlist.append({
                "code": code, "close": close, "change_pct": round(change, 2),
                "stop_high": stop_high, "volume": fnum(row.get("Volume")),
            })
    print(f"[ok] 一次候補(グロース・値上がり/S高): {len(shortlist)}件")
    return shortlist


def growth_universe(jq: JQuants, target: str, market_name: str):
    """listed/info から東証グロースのコード集合と名称辞書を返す。"""
    info = jq.get("/listed/info", {"date": target}, "info")
    if not info:
        info = jq.get("/listed/info", None, "info")
    codes, names = set(), {}
    for r in info:
        mkt = r.get("MarketCodeName") or r.get("MarketCode") or ""
        names[r["Code"]] = r.get("CompanyName", "")
        if market_name in str(mkt):
            codes.add(r["Code"])
    print(f"[ok] 東証{market_name} 銘柄数: {len(codes)}")
    return codes, names


def latest_fy_statement(stmts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    fy = [s for s in stmts
          if str(s.get("TypeOfCurrentPeriod", "")).upper() == "FY"]
    if not fy:
        return stmts[-1] if stmts else None
    return sorted(fy, key=lambda s: s.get("DisclosedDate", ""))[-1]


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
        "margin_long_k": None, "stop_loss": None,
        "manual_note": "",
    }

    # --- 株価履歴: MA / MACD / 出来高倍率 ---
    hist = jq.get("/prices/daily_quotes", {"code": code}, "daily_quotes")
    hist = [h for h in hist if fnum(h.get("Close")) is not None]
    hist.sort(key=lambda h: h.get("Date", ""))
    closes = [fnum(h.get("Close")) for h in hist]
    vols = [fnum(h.get("Volume")) or 0 for h in hist]
    if len(closes) >= 5:
        ma5, ma25 = sma(closes, 5), sma(closes, 25)
        ma75, ma200 = sma(closes, 75), sma(closes, 200)
        price = closes[-1]
        if None not in (ma5, ma25, ma75, ma200):
            rec["ma_perfect_order"] = (price > ma5 > ma25 > ma75 > ma200)
        if ma25:
            rec["stop_loss"] = round(min(closes[-5:]))  # 直近5日安値を逆指値目安
        rec["macd_cross"] = macd_cross_up(closes)
    if len(vols) > crit["ma_avg_window"]:
        base = sum(vols[-(crit["ma_avg_window"] + 1):-1]) / crit["ma_avg_window"]
        if base > 0 and vols[-1]:
            rec["volume_x"] = round(vols[-1] / base, 1)

    # --- 財務: 利益率 / 自己資本比率 / ROE / 増益 / タブー / 時価総額 ---
    stmts = jq.get("/fins/statements", {"code": code}, "statements")
    st = latest_fy_statement(stmts)
    if st:
        sales = fnum(st.get("NetSales"))
        op = fnum(st.get("OperatingProfit"))
        profit = fnum(st.get("Profit"))
        equity = fnum(st.get("Equity"))
        er = fnum(st.get("EquityToAssetRatio"))
        if er is not None and er < 1.5:  # 比率が小数(0-1)で来る場合に%へ
            er *= 100
        f_op = fnum(st.get("ForecastOperatingProfit"))
        shares = fnum(st.get(
            "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock"))
        treasury = fnum(st.get("NumberOfTreasuryStockAtTheEndOfFiscalYear")) or 0

        if sales and op is not None:
            rec["op_margin"] = round(op / sales, 3)
        if er is not None:
            rec["equity_ratio"] = round(er, 1)
        if equity and profit is not None and equity != 0:
            rec["roe"] = round(profit / equity * 100, 1)
        if f_op is not None and op is not None:
            rec["profit_trend"] = "増益" if f_op > op else "減益/横ばい"
        if shares:
            net_shares = shares - treasury
            rec["market_cap_oku"] = round(item["close"] * net_shares / 1e8, 1)

        # タブー判定
        reasons = []
        if rec["equity_ratio"] is not None and \
                rec["equity_ratio"] <= crit["taboo_equity_ratio_max"]:
            reasons.append(f"自己資本比率{rec['equity_ratio']}%≤{crit['taboo_equity_ratio_max']}%")
        cfs = [fnum(s.get("CashFlowsFromOperatingActivities"))
               for s in stmts
               if str(s.get("TypeOfCurrentPeriod", "")).upper() == "FY"]
        cfs = [c for c in cfs if c is not None][-3:]
        if len(cfs) == 3 and all(c < 0 for c in cfs):
            reasons.append("営業CF3期連続マイナス")
        rec["taboo_hit"] = len(reasons) > 0
        rec["taboo_reason"] = " / ".join(reasons)

    # --- 信用残(週次) ---
    mgn = jq.get("/markets/weekly_margin_interest", {"code": code},
                 "weekly_margin_interest")
    if mgn:
        mgn.sort(key=lambda m: m.get("Date", ""))
        longv = fnum(mgn[-1].get("LongMarginTradeVolume"))
        if longv is not None:
            rec["margin_long_k"] = round(longv / 1000, 1)

    return rec


# ----------------------------------------------------------------------
# ラベリング
# ----------------------------------------------------------------------
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
        rows.append(f"""<tr class="lab-{order.get(r['label'],9)}">
<td>{r['label']}</td><td>{r['code']}</td><td>{r['name']}</td>
<td class="num">{cell(r.get('change_pct'),'%')}</td>
<td class="num">{cell(r.get('volume_x'),'x')}</td>
<td>{'S高' if r.get('stop_high') else ''}</td>
<td class="num">{cell(r.get('market_cap_oku'),'億')}</td>
<td class="num">{cell(r.get('equity_ratio'),'%')}</td>
<td>{cell(r.get('profit_trend'))}</td>
<td>{'○' if r.get('ma_perfect_order') else '—' if r.get('ma_perfect_order') is None else '×'}</td>
<td>{'○' if r.get('macd_cross') else '—' if r.get('macd_cross') is None else '×'}</td>
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
    mail = os.environ.get("JQUANTS_MAILADDRESS")
    pw = os.environ.get("JQUANTS_PASSWORD")
    if not mail or not pw:
        print("[error] 環境変数 JQUANTS_MAILADDRESS / JQUANTS_PASSWORD が未設定")
        return 1
    crit = load_criteria()
    jq = JQuants(mail, pw)

    target = latest_trading_date(jq)
    if not target:
        print("[error] 取引日データが見つかりません(プランの配信遅延を確認)")
        return 1
    prev = prev_trading_date(jq, target)
    if not prev:
        print("[error] 前営業日が特定できません")
        return 1
    print(f"[ok] 対象日={target} / 前日={prev}")

    growth_codes, names = growth_universe(jq, target, crit["market_name"])
    shortlist = build_universe(jq, target, prev, growth_codes)

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
