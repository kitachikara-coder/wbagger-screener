#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest_dip.py : 「ストップ高後の押し目」シグナルの先着ブラケット検証（J-Quants v2）

設計検討書 4節（2026-09-12）の検証をそのまま実装する。
  シグナル日 t : 直近20営業日内にS高日 s（UL=1, s<t）があり、
                s以降の最高値からの下落率が DD_BAND 内、かつ 出来高枯れ比(Vo_t ÷ Vo_s) ≤ DRY_MAX
  エントリー   : 翌営業日始値（AdjO[t+1]）
  出口         : +5%利確 / -7%損切り 先着（同日両触れは損切り優先）／5営業日で時間切れ(終値)
  コスト       : 往復 0.25% 控除
  ベンチ       : ①同ユニバース無選別(全銘柄全日) ②S高後1〜20日の全日(条件なし) ③押し目条件のみ(枯れ比なし)
  感度         : 枯れ比 30%/50%、下落率帯を±1段
対象: 東証グロース＋スタンダード（master の MktNm）／期間: 直近約2年（Light/Standard）

限界: 上場廃止除外の生存者バイアス／日足近似（ザラ場の到達順序は無視・両触れはSL優先で保守）／
      スリッページ未考慮／連続シグナル日は相関サンプル（first-only も併記）
出力: docs/backtest_dip.html, docs/data/backtest_dip.json
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
MARKETS = ["グロース", "スタンダード"]
SH_WINDOW = 20            # S高日を探す直近営業日数
HOLD = 5                  # 保有営業日
TP, SL = 0.05, -0.07      # 利確 / 損切り
COST = 0.0025             # 往復コスト
DD_BAND = (-0.20, -0.10)  # 高値からの下落率（主条件）
DRY_MAX = 0.30            # 出来高枯れ比 上限（主条件）
MAX_CODES = int(os.environ.get("BT_MAX_CODES", "0"))

# 感度セル（主条件を±1段振る）
GRID = [
    ("主条件 DD-20〜-10 / 枯れ≤30%", (-0.20, -0.10), 0.30),
    ("枯れ≤50%",                   (-0.20, -0.10), 0.50),
    ("枯れ条件なし",               (-0.20, -0.10), None),
    ("DD-30〜-10 / 枯れ≤30%",      (-0.30, -0.10), 0.30),
    ("DD-15〜-5 / 枯れ≤30%",       (-0.15, -0.05), 0.30),
    ("DD-25〜-15 / 枯れ≤30%",      (-0.25, -0.15), 0.30),
]


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


def universe_codes(jq: JQuants) -> List[str]:
    info = jq.get("/equities/master")
    return [r["Code"] for r in info
            if any(m in str(r.get("MktNm", "")) for m in MARKETS)]


def bracket(adjo, adjh, adjl, adjc, i_entry: int):
    """i_entry 日の始値でエントリー。(pnl, kind) を返す。kind ∈ TP/SL/TIME。"""
    n = len(adjc)
    if i_entry >= n:
        return None
    entry = adjo[i_entry]
    if entry is None or entry <= 0:
        return None
    last = i_entry
    for j in range(HOLD):
        idx = i_entry + j
        if idx >= n:
            break
        last = idx
        h, l = adjh[idx], adjl[idx]
        if l is not None and l <= entry * (1 + SL):      # 両触れは損切り優先
            return SL - COST, "SL"
        if h is not None and h >= entry * (1 + TP):
            return TP - COST, "TP"
    c = adjc[last]
    if c is None:
        return None
    return (c / entry - 1) - COST, "TIME"


def new_stat():
    return {"n": 0, "tp": 0, "sl": 0, "sum": 0.0, "win": 0}


def add(stat, res):
    pnl, kind = res
    stat["n"] += 1
    stat["sum"] += pnl
    stat["tp"] += 1 if kind == "TP" else 0
    stat["sl"] += 1 if kind == "SL" else 0
    stat["win"] += 1 if pnl > 0 else 0


def summarize(stat):
    n = stat["n"]
    if not n:
        return {"n": 0, "tp_rate": None, "sl_rate": None, "avg_pnl": None, "win_rate": None}
    return {"n": n,
            "tp_rate": round(stat["tp"] / n * 100, 1),
            "sl_rate": round(stat["sl"] / n * 100, 1),
            "avg_pnl": round(stat["sum"] / n * 100, 2),
            "win_rate": round(stat["win"] / n * 100, 1)}


def main() -> int:
    api_key = os.environ.get("JQUANTS_API_KEY")
    if not api_key:
        print("[error] JQUANTS_API_KEY 未設定")
        return 1
    jq = JQuants(api_key)
    today = dt.datetime.now(JST).date()
    frm = (today - dt.timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    to = today.strftime("%Y-%m-%d")

    codes = universe_codes(jq)
    if MAX_CODES:
        codes = codes[:MAX_CODES]
    print(f"[ok] 対象 {MARKETS} {len(codes)} 銘柄 / 期間 {frm}〜{to}")

    stats = {
        "bench_all": new_stat(),      # 無選別（全銘柄全日）
        "post_sh_all": new_stat(),    # S高後1〜20日の全日（条件なし）
        "signal_all": new_stat(),     # 主条件（連続日も各カウント）
        "signal_first": new_stat(),   # 主条件・エピソード内の初回のみ
    }
    grid_stats = {label: new_stat() for label, _, _ in GRID}
    fetched = 0

    for idx, code in enumerate(codes, 1):
        try:
            rows = jq.get("/equities/bars/daily", {"code": code, "from": frm, "to": to})
        except Exception as e:
            print(f"  [warn] {code} 取得失敗: {e}")
            continue
        rows = [r for r in rows if fnum(r.get("AdjC")) is not None]
        rows.sort(key=lambda r: r.get("Date", ""))
        if len(rows) < SH_WINDOW + HOLD + 5:
            continue
        fetched += 1
        adjo = [fnum(r.get("AdjO")) for r in rows]
        adjh = [fnum(r.get("AdjH")) for r in rows]
        adjl = [fnum(r.get("AdjL")) for r in rows]
        adjc = [fnum(r.get("AdjC")) for r in rows]
        adjv = [fnum(r.get("AdjVo")) or 0.0 for r in rows]
        ul = [str(r.get("UL")) == "1" for r in rows]
        n = len(rows)

        first_done = set()   # エピソード(s) ごとに初回シグナルのみ数える
        for t in range(SH_WINDOW, n - 1):
            res = bracket(adjo, adjh, adjl, adjc, t + 1)
            if res is None:
                continue
            add(stats["bench_all"], res)

            # 直近20営業日内の最終S高日 s（s < t）
            s = None
            for k in range(t - 1, t - SH_WINDOW - 1, -1):
                if k >= 0 and ul[k]:
                    s = k
                    break
            if s is None:
                continue
            add(stats["post_sh_all"], res)

            hs = [h for h in adjh[s:t + 1] if h is not None]
            if not hs or adjv[s] <= 0:
                continue
            peak = max(hs)
            dd = adjc[t] / peak - 1
            dry = adjv[t] / adjv[s]

            for label, band, dry_max in GRID:
                if band[0] <= dd <= band[1] and (dry_max is None or dry <= dry_max):
                    add(grid_stats[label], res)

            if DD_BAND[0] <= dd <= DD_BAND[1] and dry <= DRY_MAX:
                add(stats["signal_all"], res)
                if s not in first_done:
                    first_done.add(s)
                    add(stats["signal_first"], res)

        if idx % 100 == 0:
            print(f"  ...{idx}/{len(codes)} 処理 (取得済 {fetched})")

    result = {
        "generated_at": dt.datetime.now(JST).isoformat(timespec="seconds"),
        "period": f"{frm}〜{to}", "markets": MARKETS,
        "codes": len(codes), "fetched": fetched,
        "params": {"sh_window": SH_WINDOW, "hold": HOLD, "tp": TP, "sl": SL, "cost": COST,
                   "dd_band": DD_BAND, "dry_max": DRY_MAX},
        "buckets": {k: summarize(v) for k, v in stats.items()},
        "grid": {label: summarize(v) for label, v in grid_stats.items()},
    }
    os.makedirs("docs/data", exist_ok=True)
    with open("docs/data/backtest_dip.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # --- HTML ---
    labels = {"bench_all": "① 無選別（全銘柄全日）", "post_sh_all": "② S高後1〜20日の全日（条件なし）",
              "signal_all": "★ 主条件（DD-20〜-10・枯れ≤30%）全日", "signal_first": "★ 主条件・エピソード初回のみ"}

    def row(name, d, hl=False):
        c = lambda v, s="": "—" if v is None else f"{v}{s}"
        return (f'<tr class="{"hl" if hl else ""}"><td>{name}</td>'
                f'<td class="num">{c(d["tp_rate"], "%")}</td><td class="num">{c(d["avg_pnl"], "%")}</td>'
                f'<td class="num">{c(d["win_rate"], "%")}</td><td class="num">{c(d["sl_rate"], "%")}</td>'
                f'<td class="num">{d["n"]:,}</td></tr>')

    main_rows = "".join(row(labels[k], result["buckets"][k], hl=k.startswith("signal"))
                        for k in ["bench_all", "post_sh_all", "signal_all", "signal_first"])
    grid_rows = "".join(row(l, result["grid"][l], hl=(i == 0)) for i, l in enumerate(result["grid"]))
    b, s_ = result["buckets"]["bench_all"], result["buckets"]["signal_all"]
    verdict = "—"
    if b["n"] and s_["n"]:
        ok_tp = (s_["tp_rate"] or 0) > (b["tp_rate"] or 0)
        ok_pnl = (s_["avg_pnl"] or -99) > (b["avg_pnl"] or -99)
        verdict = ("<b>合格</b>：+5%到達率・平均損益とも無選別を上回る → 層Aの抽出条件として採用可"
                   if (ok_tp and ok_pnl) else
                   "<b>不合格</b>：いずれかが無選別以下 → 層Aの抽出条件を見直してから実装（感度表を参照）")
    html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>S高後の押し目 ブラケット検証</title>
<style>
body{{font-family:system-ui,'Hiragino Sans',sans-serif;margin:16px;background:#0d1117;color:#e6edf3}}
h1{{font-size:18px}} h2{{font-size:15px;margin-top:22px}} .meta{{color:#8b949e;font-size:13px;margin-bottom:12px}}
table{{border-collapse:collapse;width:100%;font-size:13px;max-width:860px}}
th,td{{border:1px solid #30363d;padding:7px 9px;text-align:left}}
th{{background:#161b22}} td.num{{text-align:right}} tr.hl{{background:#13301f}}
.verdict{{margin:14px 0;padding:10px 12px;border:1px solid #30363d;border-radius:8px;background:#161b22}}
.note{{color:#8b949e;font-size:12px;margin-top:14px;line-height:1.7}} a{{color:#58a6ff}}
</style></head><body>
<h1>ストップ高後の押し目 — 先着ブラケット検証（+5%／-7%・5営業日）</h1>
<div class="meta">期間: {result['period']} ／ 東証グロース＋スタンダード {result['fetched']}銘柄 ／ 生成: {dt.datetime.now(JST).strftime('%Y-%m-%d %H:%M')} JST<br>
シグナル: 直近20営業日内にS高 → 最高値から下落率 -20〜-10% かつ 出来高枯れ比 ≤30% ／ 翌営業日始値エントリー ／ 往復コスト0.25%</div>
<div class="verdict">判定：{verdict}</div>
<h2>主結果</h2>
<table><thead><tr><th>条件</th><th>+5%到達率</th><th>平均損益</th><th>勝率</th><th>-7%到達率</th><th>n</th></tr></thead>
<tbody>{main_rows}</tbody></table>
<h2>感度（主条件を±1段）</h2>
<table><thead><tr><th>条件</th><th>+5%到達率</th><th>平均損益</th><th>勝率</th><th>-7%到達率</th><th>n</th></tr></thead>
<tbody>{grid_rows}</tbody></table>
<div class="note">
<b>読み方</b>：★がベンチ①（無選別）と②（S高後の全日）を<b>両方</b>上回って初めて、押し目条件（下落率・枯れ比）に選別力があると言える。②だけ上回るなら「S高後であること」自体の効果。<br>
感度表で主条件だけ良く前後のセルが悪ければ偶然の疑い（ナイフエッジ）。中庸の値を採る。<br>
<b>限界</b>：上場廃止除外（生存者バイアス・楽観方向）／日足近似で両触れは損切り優先（保守）／スリッページ未考慮／連続シグナル日は相関（初回のみ行を併記）。<br>
本結果は機械的検証であり投資助言ではない。最終判断は自己責任。｜ <a href="./">スクリーニングへ</a> ｜ <a href="./backtest.html">S高×フィルタ検証へ</a>
</div></body></html>"""
    with open("docs/backtest_dip.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("[ok] 出力: docs/backtest_dip.html, docs/data/backtest_dip.json")
    print(json.dumps({"buckets": result["buckets"], "grid": result["grid"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
