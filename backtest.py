#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.py : S高シグナル × フィルタ別 「+20%到達」的中率バックテスト（J-Quants v2）

検証内容:
  S高（UL=1）翌日エントリーを基準に、各シグナル日時点のフィルタを重ねて
  +20%到達率・期待値がどう変わるかを比較する。
  フィルタ（その日までに開示済みの決算＝先読み防止）:
    PO     : パーフェクトオーダー（株価>MA5>MA25>MA75>MA200）
    NoTaboo: タブー非該当（自己資本比率>30% かつ 営業CF3期連続マイナスでない）
    Fund   : 営業利益率≥13% & 自己資本比率≥40% & ROE≥8% & 増益(直近FY>前FY)
    Full   : PO かつ NoTaboo かつ Fund
対象: 東証グロース / 期間: 直近約2年（Light）
前向き評価(日足終値近似): +20%到達→+0.20 / 先に-8%→-0.08 / 期間末手仕舞い→実リターン
出力: docs/backtest.html, docs/data/backtest.json

限界: 上場廃止除外の生存バイアス／日足近似でザラ場順序無視／スリッページ等未考慮。
投資助言ではない。最終判断は自己責任。
"""

import os
import sys
import json
import time
import datetime as dt
from typing import Any, Dict, List, Optional

import requests

API_BASE = "https://api.jquants.com/v2"
JST = dt.timezone(dt.timedelta(hours=9))

LOOKBACK_DAYS = 760
FWD_WINDOWS = [5, 10, 20]
TARGET_UP = 0.20
STOP_DN = -0.08
MARKET_NAME = "グロース"
MAX_CODES = int(os.environ.get("BT_MAX_CODES", "0"))

BUCKETS = ["base", "A", "A_po", "A_notaboo", "A_fund", "A_full"]
BUCKET_LABEL = {
    "base": "ベース(全日)", "A": "S高", "A_po": "S高+PO",
    "A_notaboo": "S高+タブー除外", "A_fund": "S高+ファンダ", "A_full": "S高+全フィルタ",
}


class JQuants:
    def __init__(self, api_key: str, min_interval: float = 1.05):
        self.s = requests.Session()
        self.key = api_key
        self.min_interval = min_interval
        self._last = 0.0

    def _throttle(self):
        gap = time.time() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.time()

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        params = dict(params or {})
        headers = {"x-api-key": self.key}
        out: List[Dict[str, Any]] = []
        for _ in range(300):
            self._throttle()
            for attempt in range(4):
                r = self.s.get(f"{API_BASE}{path}", params=params, headers=headers, timeout=60)
                if r.status_code == 200:
                    break
                if r.status_code in (429, 500, 502, 503):
                    time.sleep(2 * (attempt + 1))
                    continue
                r.raise_for_status()
            else:
                r.raise_for_status()
            body = r.json()
            out.extend(body.get("data", []) or [])
            pk = body.get("pagination_key")
            if not pk:
                break
            params["pagination_key"] = pk
        return out


def fnum(x):
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def sma_at(values: List[float], i: int, n: int):
    if i + 1 < n:
        return None
    seg = values[i - n + 1:i + 1]
    if any(v is None for v in seg):
        return None
    return sum(seg) / n


def growth_codes(jq: JQuants) -> List[str]:
    info = jq.get("/equities/master")
    return [r["Code"] for r in info if MARKET_NAME in str(r.get("MktNm", ""))]


def forward_outcome(adjc: List[float], i: int, k: int):
    entry = adjc[i]
    if entry is None or entry <= 0:
        return None
    reach20 = False
    outcome = None
    for j in range(1, k + 1):
        if i + j >= len(adjc):
            break
        c = adjc[i + j]
        if c is None:
            continue
        r = c / entry - 1
        if r >= TARGET_UP:
            reach20 = True
            if outcome is None:
                outcome = TARGET_UP
            break
        if r <= STOP_DN and outcome is None:
            outcome = STOP_DN
            break
    if outcome is None:
        last = None
        for j in range(min(k, len(adjc) - 1 - i), 0, -1):
            if adjc[i + j] is not None:
                last = adjc[i + j]
                break
        outcome = (last / entry - 1) if last else 0.0
    return reach20, outcome


def fy_summary(stmts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    fy = [s for s in stmts if str(s.get("CurPerType", "")).upper() == "FY"]
    fy.sort(key=lambda s: s.get("DiscDate", ""))
    return fy


def eval_filters(fys_asof: List[Dict[str, Any]], adjc, ma_arrays, i):
    ma5, ma25, ma75, ma200 = ma_arrays
    price = adjc[i]
    po = (None not in (ma5[i], ma25[i], ma75[i], ma200[i]) and price is not None
          and price > ma5[i] > ma25[i] > ma75[i] > ma200[i])
    fund_ok = False
    notaboo_ok = False
    if fys_asof:
        st = fys_asof[-1]
        sales, op, np_ = fnum(st.get("Sales")), fnum(st.get("OP")), fnum(st.get("NP"))
        eq, eqar = fnum(st.get("Eq")), fnum(st.get("EqAR"))
        eqar = eqar * 100 if eqar is not None else None
        opm = (op / sales) if (sales and op is not None) else None
        roe = (np_ / eq * 100) if (eq and np_ is not None and eq != 0) else None
        ops = [fnum(r.get("OP")) for r in fys_asof if fnum(r.get("OP")) is not None]
        up = len(ops) >= 2 and ops[-1] > ops[-2]
        if None not in (opm, eqar, roe):
            fund_ok = (opm >= 0.13 and eqar >= 40 and roe >= 8 and up)
        cfs = [fnum(r.get("CFO")) for r in fys_asof if fnum(r.get("CFO")) is not None][-3:]
        taboo = (eqar is not None and eqar <= 30) or (len(cfs) == 3 and all(c < 0 for c in cfs))
        notaboo_ok = (eqar is not None) and (not taboo)
    return po, fund_ok, notaboo_ok


def main() -> int:
    api_key = os.environ.get("JQUANTS_API_KEY")
    if not api_key:
        print("[error] JQUANTS_API_KEY 未設定")
        return 1
    jq = JQuants(api_key)

    today = dt.datetime.now(JST).date()
    frm = (today - dt.timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    to = today.strftime("%Y-%m-%d")

    codes = growth_codes(jq)
    if MAX_CODES:
        codes = codes[:MAX_CODES]
    print(f"[ok] 対象 東証グロース {len(codes)} 銘柄 / 期間 {frm}〜{to}")

    stats = {b: {w: {"n": 0, "reach": 0, "exp": 0.0} for w in FWD_WINDOWS} for b in BUCKETS}

    for idx, code in enumerate(codes, 1):
        try:
            rows = jq.get("/equities/bars/daily", {"code": code, "from": frm, "to": to})
        except Exception as e:
            print(f"  [warn] {code} bars失敗: {e}")
            continue
        rows = [r for r in rows if fnum(r.get("AdjC")) is not None]
        rows.sort(key=lambda r: r.get("Date", ""))
        if len(rows) < 200 + max(FWD_WINDOWS) + 2:
            continue
        dates = [r.get("Date", "") for r in rows]
        adjc = [fnum(r.get("AdjC")) for r in rows]
        ul = [str(r.get("UL")) for r in rows]
        ma5 = [sma_at(adjc, i, 5) for i in range(len(adjc))]
        ma25 = [sma_at(adjc, i, 25) for i in range(len(adjc))]
        ma75 = [sma_at(adjc, i, 75) for i in range(len(adjc))]
        ma200 = [sma_at(adjc, i, 200) for i in range(len(adjc))]
        ma_arrays = (ma5, ma25, ma75, ma200)

        try:
            stmts = jq.get("/fins/summary", {"code": code})
        except Exception:
            stmts = []
        fys = fy_summary(stmts)

        for i in range(200, len(rows) - 1):
            # ベース
            for w in FWD_WINDOWS:
                if i + w < len(rows):
                    res = forward_outcome(adjc, i, w)
                    if res:
                        stats["base"][w]["n"] += 1
                        stats["base"][w]["reach"] += 1 if res[0] else 0
            if ul[i] != "1":
                continue
            # シグナル日までに開示済みの FY のみ（先読み防止）
            asof = [s for s in fys if s.get("DiscDate", "") <= dates[i]]
            po, fund_ok, notaboo_ok = eval_filters(asof, adjc, ma_arrays, i)
            fired = {"A": True, "A_po": po, "A_notaboo": notaboo_ok,
                     "A_fund": fund_ok, "A_full": (po and fund_ok and notaboo_ok)}
            for w in FWD_WINDOWS:
                if i + w >= len(rows):
                    continue
                res = forward_outcome(adjc, i, w)
                if not res:
                    continue
                for b, ok in fired.items():
                    if ok:
                        stats[b][w]["n"] += 1
                        stats[b][w]["reach"] += 1 if res[0] else 0
                        stats[b][w]["exp"] += res[1]
        if idx % 50 == 0:
            print(f"  ...{idx}/{len(codes)} 処理")

    def rate(d):
        return round(d["reach"] / d["n"] * 100, 1) if d["n"] else None

    def expv(d):
        return round(d["exp"] / d["n"] * 100, 2) if d["n"] else None

    result = {"generated_at": dt.datetime.now(JST).isoformat(timespec="seconds"),
              "period": f"{frm}〜{to}", "codes": len(codes),
              "params": {"target_up": TARGET_UP, "stop_dn": STOP_DN},
              "buckets": {}}
    for b in BUCKETS:
        result["buckets"][b] = {"label": BUCKET_LABEL[b],
                                "w": {w: {"n": stats[b][w]["n"], "reach20_pct": rate(stats[b][w]),
                                          "expectancy_pct": expv(stats[b][w])} for w in FWD_WINDOWS}}

    os.makedirs("docs/data", exist_ok=True)
    with open("docs/data/backtest.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # HTML
    head = "".join(f"<th>{w}日<br>到達/期待値</th>" for w in FWD_WINDOWS)
    body = []
    for b in BUCKETS:
        cells = []
        for w in FWD_WINDOWS:
            d = result["buckets"][b]["w"][w]
            rp = "—" if d["reach20_pct"] is None else f"{d['reach20_pct']}%"
            ep = "—" if d["expectancy_pct"] is None else f"{d['expectancy_pct']}%"
            cells.append(f'<td class="num">{rp}<br><span class="sub">{ep} / n={d["n"]:,}</span></td>')
        cls = "hl" if b == "A_full" else ("base" if b == "base" else "")
        body.append(f'<tr class="{cls}"><td>{result["buckets"][b]["label"]}</td>{"".join(cells)}</tr>')

    html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>S高×フィルタ別 +20%到達 バックテスト</title>
<style>
body{{font-family:system-ui,'Hiragino Sans',sans-serif;margin:16px;background:#0d1117;color:#e6edf3}}
h1{{font-size:18px}} .meta{{color:#8b949e;font-size:13px;margin-bottom:12px}}
table{{border-collapse:collapse;width:100%;font-size:13px;max-width:720px}}
th,td{{border:1px solid #30363d;padding:7px 9px;text-align:left}}
th{{background:#161b22}} td.num{{text-align:right}}
tr.hl{{background:#13301f}} tr.base{{color:#8b949e}}
.sub{{color:#8b949e;font-size:11px}} .note{{color:#8b949e;font-size:12px;margin-top:14px;line-height:1.7}}
a{{color:#58a6ff}}
</style></head><body>
<h1>S高シグナル × フィルタ別　+20%到達 的中率</h1>
<div class="meta">期間: {result['period']} ／ 東証グロース {result['codes']}銘柄 ／ 生成: {dt.datetime.now(JST).strftime('%Y-%m-%d %H:%M')} JST<br>
目標+20% / 損切り-8% ／ 各セル＝[+20%到達率] ／ [期待値 / 母数n]</div>
<table><thead><tr><th>条件</th>{head}</tr></thead><tbody>
{''.join(body)}
</tbody></table>
<div class="note">
<b>読み方</b>：到達率がベースを大きく上回り、フィルタを重ねるほど上がれば、その条件にエッジがある。期待値（-8%損切り込み1トレード平均）がプラスなら逆指値運用で優位。母数nが小さいフィルタは統計的にブレる点に注意。<br>
フィルタはシグナル日までに開示済みの決算のみで判定（先読み防止）。<br>
<b>限界</b>：上場廃止除外の生存バイアス／日足終値近似／スリッページ・流動性・寄り価格未考慮／グロースのみ・直近約2年。<br>
本結果は機械的検証であり投資助言ではない。最終判断は自己責任。 ｜ <a href="./">スクリーニングへ戻る</a>
</div></body></html>"""
    with open("docs/backtest.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("[ok] 出力: docs/backtest.html, docs/data/backtest.json")
    print(json.dumps({b: result["buckets"][b]["w"] for b in BUCKETS}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
