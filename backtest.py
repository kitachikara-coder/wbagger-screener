#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.py : 「初動シグナル → 前向き20%上昇」的中率バックテスト（J-Quants v2）

仮説検証:
  値上がり率/出来高急増/S高 の初動シグナルが出た銘柄は、その後 K営業日以内に
  +20% に到達するか? 的中率・期待値を、ベースレート(全営業日平均)と比較する。

対象: 東証グロース（直近 master 基準）/ 期間: 直近約 LOOKBACK_DAYS 日（Light=約2年）
シグナル:
  A) S高（UL=1）
  B) 出来高急増(≥VOL_X×20日平均) かつ 前日比≥CHG_MIN%
前向き評価(日足終値ベース近似):
  エントリー = シグナル日の調整後終値。以後 K 日で
   - 終値が +20% 到達 → 的中(+0.20)
   - 先に -8% 到達 → 損切り(-0.08)
   - どちらも無ければ K 日後終値で手仕舞い(実リターン)
出力: docs/backtest.html, docs/data/backtest.json

注意/限界:
  - 上場廃止銘柄は現 master に無く除外 → 生存バイアスで的中率はやや楽観
  - 日足終値近似（ザラ場の到達順序は無視）
  - スリッページ・流動性・寄り価格は未考慮
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

# --- パラメータ ---
LOOKBACK_DAYS = 730       # 取得期間（日）
FWD_WINDOWS = [5, 10, 20]  # 前向き評価日数
TARGET_UP = 0.20          # 目標上昇率
STOP_DN = -0.08           # 損切り
VOL_X = 3.0               # 出来高急増倍率
VOL_WIN = 20              # 出来高平均期間
CHG_MIN = 0.05            # 出来高急増シグナルの最低前日比
MARKET_NAME = "グロース"
MAX_CODES = int(os.environ.get("BT_MAX_CODES", "0"))  # 0=全銘柄（テスト時に制限可）


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


def growth_codes(jq: JQuants) -> List[str]:
    info = jq.get("/equities/master")
    codes = [r["Code"] for r in info if MARKET_NAME in str(r.get("MktNm", ""))]
    return codes


def forward_outcome(adjc: List[float], i: int, k: int):
    """i 日エントリーで前向き k 日。(reach20, outcome) を返す。"""
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
        # K日後終値で手仕舞い
        last = None
        for j in range(min(k, len(adjc) - 1 - i), 0, -1):
            if adjc[i + j] is not None:
                last = adjc[i + j]
                break
        outcome = (last / entry - 1) if last else 0.0
    return reach20, outcome


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

    # 集計器
    stats = {
        "A_stophigh": {w: {"n": 0, "reach": 0, "exp": 0.0} for w in FWD_WINDOWS},
        "B_volspike": {w: {"n": 0, "reach": 0, "exp": 0.0} for w in FWD_WINDOWS},
        "base": {w: {"n": 0, "reach": 0} for w in FWD_WINDOWS},
    }

    for idx, code in enumerate(codes, 1):
        try:
            rows = jq.get("/equities/bars/daily", {"code": code, "from": frm, "to": to})
        except Exception as e:
            print(f"  [warn] {code} 取得失敗: {e}")
            continue
        rows = [r for r in rows if fnum(r.get("AdjC")) is not None]
        rows.sort(key=lambda r: r.get("Date", ""))
        if len(rows) < VOL_WIN + max(FWD_WINDOWS) + 2:
            continue
        adjc = [fnum(r.get("AdjC")) for r in rows]
        adjvo = [fnum(r.get("AdjVo")) or 0 for r in rows]
        ul = [str(r.get("UL")) for r in rows]

        for i in range(VOL_WIN, len(rows) - 1):
            # ベースレート（全日）
            for w in FWD_WINDOWS:
                if i + w < len(rows):
                    res = forward_outcome(adjc, i, w)
                    if res:
                        stats["base"][w]["n"] += 1
                        if res[0]:
                            stats["base"][w]["reach"] += 1
            # シグナルA: S高
            sig_a = ul[i] == "1"
            # シグナルB: 出来高急増＋値上がり
            vbase = sum(adjvo[i - VOL_WIN:i]) / VOL_WIN if VOL_WIN else 0
            chg = (adjc[i] / adjc[i - 1] - 1) if adjc[i - 1] else 0
            sig_b = vbase > 0 and adjvo[i] >= VOL_X * vbase and chg >= CHG_MIN
            for key, fired in (("A_stophigh", sig_a), ("B_volspike", sig_b)):
                if not fired:
                    continue
                for w in FWD_WINDOWS:
                    if i + w >= len(rows):
                        continue
                    res = forward_outcome(adjc, i, w)
                    if not res:
                        continue
                    stats[key][w]["n"] += 1
                    stats[key][w]["reach"] += 1 if res[0] else 0
                    stats[key][w]["exp"] += res[1]
        if idx % 50 == 0:
            print(f"  ...{idx}/{len(codes)} 処理")

    # 集計
    def rate(d):
        return round(d["reach"] / d["n"] * 100, 1) if d["n"] else None

    result = {"generated_at": dt.datetime.now(JST).isoformat(timespec="seconds"),
              "period": f"{frm}〜{to}", "codes": len(codes),
              "params": {"target_up": TARGET_UP, "stop_dn": STOP_DN,
                         "vol_x": VOL_X, "chg_min": CHG_MIN},
              "windows": {}}
    for w in FWD_WINDOWS:
        a, b, base = stats["A_stophigh"][w], stats["B_volspike"][w], stats["base"][w]
        result["windows"][w] = {
            "base_reach20_pct": rate(base), "base_n": base["n"],
            "A_stophigh": {"n": a["n"], "reach20_pct": rate(a),
                           "expectancy_pct": round(a["exp"] / a["n"] * 100, 2) if a["n"] else None},
            "B_volspike": {"n": b["n"], "reach20_pct": rate(b),
                           "expectancy_pct": round(b["exp"] / b["n"] * 100, 2) if b["n"] else None},
        }

    os.makedirs("docs/data", exist_ok=True)
    with open("docs/data/backtest.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # HTML
    rows_html = []
    for w in FWD_WINDOWS:
        d = result["windows"][w]
        rows_html.append(f"""<tr><td>{w}日</td>
<td class="num">{d['base_reach20_pct']}%<br><span class="sub">n={d['base_n']:,}</span></td>
<td class="num hl">{d['A_stophigh']['reach20_pct']}%<br><span class="sub">n={d['A_stophigh']['n']:,}</span></td>
<td class="num">{d['A_stophigh']['expectancy_pct']}%</td>
<td class="num hl">{d['B_volspike']['reach20_pct']}%<br><span class="sub">n={d['B_volspike']['n']:,}</span></td>
<td class="num">{d['B_volspike']['expectancy_pct']}%</td></tr>""")
    html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>20%上昇 的中率バックテスト</title>
<style>
body{{font-family:system-ui,'Hiragino Sans',sans-serif;margin:16px;background:#0d1117;color:#e6edf3}}
h1{{font-size:18px}} .meta{{color:#8b949e;font-size:13px;margin-bottom:12px}}
table{{border-collapse:collapse;width:100%;font-size:13px;max-width:760px}}
th,td{{border:1px solid #30363d;padding:7px 9px;text-align:left}}
th{{background:#161b22}} td.num{{text-align:right}} td.hl{{background:#13251c}}
.sub{{color:#8b949e;font-size:11px}} .note{{color:#8b949e;font-size:12px;margin-top:14px;line-height:1.7}}
a{{color:#58a6ff}}
</style></head><body>
<h1>初動シグナル → +20%到達 的中率バックテスト</h1>
<div class="meta">期間: {result['period']} ／ 対象: 東証グロース {result['codes']}銘柄 ／ 生成: {dt.datetime.now(JST).strftime('%Y-%m-%d %H:%M')} JST<br>
目標+20% / 損切り-8% / 出来高急増={VOL_X}倍・前日比≥{int(CHG_MIN*100)}%</div>
<table><thead><tr>
<th>前向き</th><th>ベース<br>(全日)+20%到達</th><th>A:S高<br>+20%到達</th><th>A:期待値</th>
<th>B:出来高急増<br>+20%到達</th><th>B:期待値</th></tr></thead>
<tbody>{''.join(rows_html)}</tbody></table>
<div class="note">
<b>読み方</b>：「+20%到達」は、シグナル日の翌日以降そのN日以内に終値が+20%に達した割合（生の予測力）。「期待値」は +20%到達で+20%・先に-8%で損切り・どちらも無ければN日後終値で手仕舞いとした1トレード平均リターン。<br>
シグナルの到達率が<b>ベース(全日平均)を上回るほどエッジがある</b>。期待値がプラスなら逆指値運用で優位。<br>
<b>限界</b>：上場廃止銘柄は除外（生存バイアスで楽観方向）／日足終値近似でザラ場の到達順序は無視／スリッページ・流動性未考慮／期間は直近約2年（Light）。<br>
本結果は機械的検証であり投資助言ではない。最終判断は自己責任。 ｜ <a href="./">スクリーニングへ戻る</a>
</div></body></html>"""
    with open("docs/backtest.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("[ok] 出力: docs/backtest.html, docs/data/backtest.json")
    print(json.dumps(result["windows"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
