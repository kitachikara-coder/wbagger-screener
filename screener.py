#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wbagger-screener : J-Quants API v2 ベースの「ストップ高後の押し目」スクリーニング
- 認証: APIキー方式（x-api-key ヘッダ。ダッシュボードで発行）
- 対象市場: 東証グロース＋スタンダード（criteria.yaml の markets で変更可）
- 母集団 = 直近20営業日内にストップ高(UL=1)があった銘柄（当日S高も含む）
- 層A（ふるい一覧）: S高日 / 高値からの日数・下落率 / 出来高枯れ比 / 売買代金 などの「事実」だけを並べる
- 層B（銘柄カード）: チャートで見えない情報（材料・需給・決算日・過去の類似局面）
- docs/data/latest.json と docs/index.html を生成

**合成スコア・◎○△の総合判定は作らない。** 買うべきかを機械に決めさせず、
本人が「今この銘柄を自分なら買うか」を判断するための材料を並べるのが目的。

J-Quants v2 列名(実測): bars/daily(C,O,H,L,UL,LL,Vo,Va,MktCap,AdjO,AdjH,AdjL,AdjC,AdjVo) /
  master(CoName,MktNm,S33,S33Nm,S17,S17Nm) /
  fins/summary(CurPerType,DiscDate,Sales,OP,NP,Eq,EqAR,CFO,FOP,ShOutFY,TrShFY) /
  markets/margin-interest(Date,Code,IssType,LongVol,ShrtVol,LongStdVol,ShrtStdVol,LongNegVol,ShrtNegVol)
投資助言ではない。最終判断は自己責任。
"""

import os
import sys
import json
import time
import unicodedata
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    import yaml
except ImportError:
    yaml = None

API_BASE = "https://api.jquants.com/v2"
JST = dt.timezone(dt.timedelta(hours=9))
CHART_POINTS = 120  # チャート表示日数

HERE = os.path.dirname(os.path.abspath(__file__))
MANUAL_DIR = os.path.join(HERE, "manual")       # 手入力 manual/{code}.yaml
DOCS_DIR = os.path.join(HERE, "docs")
NA = "—"                 # 算出不能・取得不可（U+2014）。手入力待ちの「未」とは区別する
MATERIAL_UNSET = "未"    # 材料分類の未入力

# 材料分類（手入力の選択肢。表示順もこの順）
MATERIAL_CLASSES = ["上方修正", "大型契約・提携", "テーマ連想", "仕手・材料不明"]

# 論理APIコール数（jq.get 1回=1。内部ページングは含まない。実測用）
REQ = {"n": 0}

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
    # --- ストップ高押し目版で追加（既存キーは後方互換のため削除しない） ---
    "sh_window": 20,              # S高日を探す直近営業日数（追加API = この日数ぶん）
    "earnings_skip_bdays": 5,     # 「決算跨ぎ除外」フィルタの既定日数（フィルタ自体は既定OFF）
    # B群ゲート: backtest_dip.py が「+5%到達率・平均損益とも無選別ベンチ超え」を
    # 満たすまで true にしないこと（docs/handover/Code依頼_..._20260912.md V0節）
    "dip_default_filter": False,
    "dip_dd_band": [-20.0, -10.0],  # 上記が true のときだけ使う下落率レンジ(%)
    "dip_dry_max": 30.0,            # 上記が true のときだけ使う出来高枯れ比 上限(%)
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
def api(jq: JQuants, path: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """jq.get の薄いラッパ。論理リクエスト数を数えるだけ（JQuants本体は凍結）。"""
    REQ["n"] += 1
    return jq.get(path, params)


def bars_by_date(jq: JQuants, date_str: str,
                 cache: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> List[Dict[str, Any]]:
    """日付一括の日足。cache を渡すと同一日の再取得をしない（target/prev の重複取得を防ぐ）。"""
    if cache is not None and date_str in cache:
        return cache[date_str]
    rows = api(jq, "/equities/bars/daily", {"date": date_str})
    if cache is not None:
        cache[date_str] = rows
    return rows


def latest_trading_date(jq: JQuants, max_back: int = 8, cache=None) -> Optional[str]:
    # 当日(夕方の更新後)から遡って、最新の取引日を探す
    today = dt.datetime.now(JST).date()
    for i in range(0, max_back + 2):
        ds = (today - dt.timedelta(days=i)).strftime("%Y-%m-%d")
        if bars_by_date(jq, ds, cache):
            return ds
    return None


def prev_trading_date(jq: JQuants, base: str, cache=None) -> Optional[str]:
    b = dt.datetime.strptime(base, "%Y-%m-%d").date()
    for i in range(1, 9):
        ds = (b - dt.timedelta(days=i)).strftime("%Y-%m-%d")
        if bars_by_date(jq, ds, cache):
            return ds
    return None


def normalize_sector(name: Any) -> str:
    """33業種名の正規化。master の S33Nm は半角中黒（例「情報･通信業」）で返ることがある。"""
    if not name:
        return ""
    return unicodedata.normalize("NFKC", str(name)).strip().replace("･", "・")


def market_universe(jq: JQuants, target: str, markets: List[str]):
    """(対象市場のコード集合, 全銘柄の社名, 対象市場の市場名, 全銘柄の33業種名) を返す。

    社名・業種は市場を問わず全銘柄ぶん持つ（層Bのセクター連動が全市場を母数にするため）。
    """
    info = api(jq, "/equities/master", {"date": target})
    if not info:
        info = api(jq, "/equities/master")
    codes, names, mkt, sec = set(), {}, {}, {}
    for r in info:
        names[r["Code"]] = r.get("CoName", "")
        sec[r["Code"]] = normalize_sector(r.get("S33Nm"))
        mn = str(r.get("MktNm", ""))
        if any(m in mn for m in markets):
            codes.add(r["Code"])
            mkt[r["Code"]] = mn
    print(f"[ok] 対象市場 {markets} 銘柄数: {len(codes)} / master総数: {len(names)}")
    return codes, names, mkt, sec


def recent_stop_high(jq: JQuants, uni_codes: set, target: str, window: int = 20,
                     cache=None) -> Dict[str, Dict[str, Any]]:
    """直近 window 営業日（target を含む）に UL=1 があった銘柄を集める。

    戻り値 {code: {"sh_date","sh_vol","sh_close"}}。新しい日から遡るので、
    同一銘柄が複数回S高していれば **最新のS高日** が採用される。
    追加APIは「実際に走査した営業日数 − キャッシュ済みの日数」。
    """
    found: Dict[str, Dict[str, Any]] = {}
    base = dt.datetime.strptime(target, "%Y-%m-%d").date()
    scanned: List[str] = []
    # 土日は問い合わせない。祝日連休で空振りしても走査が止まらないよう暦日に余裕を持たせる
    budget = window * 2 + 12
    i = 0
    while len(scanned) < window and i < budget:
        d = base - dt.timedelta(days=i)
        i += 1
        if d.weekday() >= 5:          # 5=土, 6=日
            continue
        ds = d.strftime("%Y-%m-%d")
        rows = bars_by_date(jq, ds, cache)
        if not rows:                  # 祝日・未配信
            continue
        scanned.append(ds)
        for r in rows:
            code = r.get("Code")
            if not code or code in found:
                continue
            if uni_codes and code not in uni_codes:
                continue
            if str(r.get("UL")) != "1":
                continue
            found[code] = {"sh_date": ds,
                           "sh_vol": fnum(r.get("AdjVo")),
                           "sh_close": fnum(r.get("AdjC"))}
    print(f"[ok] S高探索: 直近{len(scanned)}営業日({scanned[-1] if scanned else '—'}〜{target}) "
          f"→ {len(found)}銘柄")
    if len(scanned) < window:
        print(f"  [warn] 走査できた営業日が {len(scanned)}/{window} 日にとどまった（暦日上限）")
    return found


def dip_metrics(dates: List[str], highs: List[Optional[float]], closes: List[Optional[float]],
                vols: List[Optional[float]], sh_date: str) -> Dict[str, Any]:
    """S高日を起点にした押し目の熟成度。日足は昇順、最終要素が当日。

    - 高値からの日数 : S高日以降の最高値を **最後に** 付けた日から当日までの営業日数（添字差）
    - 高値からの下落率: 当日終値 ÷ 期間最高値 − 1（%）
    - 出来高枯れ比   : 当日出来高 ÷ S高日出来高（%）／ 直近5日平均 ÷ S高日（%）
    S高日当日は 日数0・下落率0(終値=高値のとき)・枯れ比100% になる。
    算出できない項目は None（0% と誤表示しないこと）。
    """
    out = {"sh_idx": None, "peak": None, "peak_date": None,
           "days_from_peak": None, "dd_pct": None, "dry_pct": None, "dry5_pct": None}
    if not dates or sh_date not in dates:
        return out
    s = len(dates) - 1 - dates[::-1].index(sh_date)   # 同一日が重複しても最後を採る
    t = len(dates) - 1
    if s > t:
        return out
    out["sh_idx"] = s

    peak, peak_idx = None, None
    for i in range(s, t + 1):
        h = highs[i] if i < len(highs) else None
        if h is None:
            continue
        if peak is None or h >= peak:     # 同値なら新しい方（=最後に高値を付けた日）
            peak, peak_idx = h, i
    if peak is not None and peak > 0:
        out["peak"], out["peak_date"] = peak, dates[peak_idx]
        out["days_from_peak"] = t - peak_idx
        c = closes[t] if t < len(closes) else None
        if c is not None:
            out["dd_pct"] = (c / peak - 1) * 100

    shv = vols[s] if s < len(vols) else None
    if shv:                                # 0 / None はゼロ除算を避けて None
        cv = vols[t] if t < len(vols) else None
        if cv is not None:
            out["dry_pct"] = cv / shv * 100
        last5 = [v for v in vols[max(0, t - 4):t + 1] if v is not None]
        if last5:
            out["dry5_pct"] = (sum(last5) / len(last5)) / shv * 100
    return out


def turnover_oku20(closes: List[Optional[float]], vols: List[Optional[float]],
                   window: int = 20) -> Optional[float]:
    """20日平均売買代金(億円)。定義は本体 kabu-monitor と同じ mean(AdjC × AdjVo, 20日)。
       当日を含む直近20本。20本に満たなければ None。"""
    n = min(len(closes), len(vols))
    if n < window:
        return None
    vals = []
    for i in range(n - window, n):
        c, v = closes[i], vols[i]
        if c is None or v is None:
            return None
        vals.append(c * v)
    return sum(vals) / window / 1e8


def bdays_between(d0: dt.date, d1: dt.date) -> Optional[int]:
    """d0 の翌営業日を1日目とした d1 までの営業日数。**土日のみ考慮・祝日未対応**（多めに出る）。
       d1 == d0 は 0、過去日は None。"""
    if not isinstance(d0, dt.date) or not isinstance(d1, dt.date) or d1 < d0:
        return None
    n, d = 0, d0
    while d < d1:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _norm_date(v: Any) -> Optional[dt.date]:
    """YAML値・API値を date に正規化（date/datetime/'YYYY-MM-DD' のどれでも受ける）。"""
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v).strip())
    except (ValueError, TypeError):
        return None


def load_manual(code: str, base_dir: Optional[str] = None) -> Dict[str, Any]:
    """manual/{4桁コード}.yaml を読む。無ければ/読めなければ {}（画面は「未」「—」で埋まる）。
       earnings_date は date に正規化して返す。"""
    path = os.path.join(base_dir or MANUAL_DIR, "%s.yaml" % disp_code(str(code).strip()))
    if yaml is None or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            m = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"  [warn] {path} 読込失敗({e})。手入力は無視します")
        return {}
    if not isinstance(m, dict):
        print(f"  [warn] {path} の最上位が辞書ではありません。手入力は無視します")
        return {}
    if "earnings_date" in m:
        m["earnings_date"] = _norm_date(m["earnings_date"])
    return m


def pick_next_earnings(rows: List[Dict[str, Any]], today: dt.date) -> Dict[str, str]:
    """earnings-calendar の行から {Code: 'YYYY-MM-DD'}。同一コードが複数あれば
    **最も近い未来（today 以降）** を採る。未来の予定が1件も無いコードは載せない。"""
    cal: Dict[str, str] = {}
    for r in rows or []:
        d, c = _norm_date(r.get("Date")), str(r.get("Code") or "").strip()
        if not d or not c or d < today:
            continue
        iso = d.isoformat()
        if c not in cal or iso < cal[c]:
            cal[c] = iso
    return cal


def earnings_map(jq: JQuants, today: Optional[dt.date] = None) -> Dict[str, str]:
    """決算発表予定 {5桁Code: 'YYYY-MM-DD'}。API 1回。

    ⚠ 実測(2026-09-12): /equities/earnings-calendar は date/from/to/code を**全て無視**し、
    翌営業日発表分だけを返す限定フィード。よって「収録が無い＝発表予定なし」ではない。
    層Aの「決算日まで」は手入力 manual/{code}.yaml の earnings_date が主で、本APIは補助。
    """
    today = today or dt.datetime.now(JST).date()
    try:
        rows = api(jq, "/equities/earnings-calendar", {})
    except Exception as e:
        print(f"[warn] earnings-calendar 取得失敗: {e}")
        return {}
    cal = pick_next_earnings(rows, today)
    print(f"[ok] 決算発表予定(翌営業日分のみの限定フィード): 受信{len(rows or [])}件 → 未来{len(cal)}件")
    return cal


def days_to_earnings(edate: Any, today: dt.date) -> Optional[int]:
    """決算発表日までの営業日数。予定日なし／過去日は None（=不明）。当日は 0。"""
    d = _norm_date(edate)
    if d is None:
        return None
    return bdays_between(today, d)


def build_shortlist(jq: JQuants, target: str, prev: str, uni_codes: set,
                    sh_map: Dict[str, Dict[str, Any]], crit: Dict[str, Any],
                    cache=None) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
    """母集団 = 直近S高銘柄（sh_map）。当日バーが無い銘柄（売買停止等）は落とす。

    併せて当日の前日比を全銘柄ぶん計算して返す（層Bのセクター連動で使う・追加API 0）。
    """
    cur = {r["Code"]: r for r in bars_by_date(jq, target, cache)}
    prv = {r["Code"]: r for r in bars_by_date(jq, prev, cache)}
    chg_all: Dict[str, float] = {}
    for code, row in cur.items():
        c, p = fnum(row.get("C")), fnum(prv.get(code, {}).get("C"))
        if c is not None and p:
            chg_all[code] = (c - p) / p * 100

    shortlist, dropped = [], 0
    for code, sh in sh_map.items():
        row = cur.get(code)
        if row is None:
            dropped += 1
            continue
        close = fnum(row.get("C"))
        if close is None:
            dropped += 1
            continue
        shortlist.append({"code": code, "close": close,
                          "change_pct": (round(chg_all[code], 2) if code in chg_all else None),
                          "stop_high": str(row.get("UL")) == "1",
                          "volume": fnum(row.get("Vo")),
                          "sh_date": sh["sh_date"], "sh_vol": sh.get("sh_vol"),
                          "sh_close": sh.get("sh_close")})
    today_sh = sum(1 for s in shortlist if s["sh_date"] == target)
    print(f"[ok] 母集団(直近{crit['sh_window']}営業日にS高): {len(shortlist)}件"
          f"（うち当日S高 {today_sh}件 / 押し目進行中 {len(shortlist) - today_sh}件"
          f"{f' / 当日バー無しで除外 {dropped}件' if dropped else ''}）")
    return shortlist, chg_all


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
                      mkt: Dict[str, str], crit: Dict[str, Any],
                      sec: Optional[Dict[str, str]] = None,
                      ecal: Optional[Dict[str, str]] = None,
                      manual_dir: Optional[str] = None) -> Dict[str, Any]:
    code = item["code"]
    today = dt.datetime.strptime(item["date_target"], "%Y-%m-%d").date()
    rec = {
        "code": disp_code(code), "raw_code": code,
        "name": names.get(code, ""), "market": mkt.get(code, ""),
        "sector": (sec or {}).get(code, ""),
        "price": item["close"], "change_pct": item["change_pct"],
        "stop_high": item["stop_high"],
        # --- 層A: 押し目の熟成度（事実のみ） ---
        "sh_date": item.get("sh_date"), "sh_vol": item.get("sh_vol"),
        "days_from_peak": None, "dd_pct": None, "dry_pct": None, "dry5_pct": None,
        "peak": None, "peak_date": None, "turnover_oku": None,
        "earn_date": None, "earn_src": None, "earn_bdays": None,
        "material_class": MATERIAL_UNSET,
        # --- 層B（チャートで見えない情報。P2で埋める） ---
        "volume_x": None, "market_cap_oku": None,
        "ma_perfect_order": None, "macd_cross": None,
        "op_margin": None, "equity_ratio": None, "roe": None,
        "profit_trend": None, "taboo_hit": None, "taboo_reason": "",
        "margin_long_k": None, "stop_loss": None, "manual_note": "",
        "note_data": "", "chart": [],
    }

    manual = load_manual(code, manual_dir)
    mc = str(manual.get("material_class") or "").strip()
    rec["material_class"] = mc if mc else MATERIAL_UNSET

    # 決算発表日: 手入力を優先し、無ければ earnings-calendar（翌営業日分のみの限定フィード）
    m_ed = _norm_date(manual.get("earnings_date"))
    if m_ed is not None:
        rec["earn_date"] = m_ed.isoformat()
        rec["earn_src"] = "確定" if manual.get("earnings_date_confirmed") else "推定"
    elif (ecal or {}).get(code):
        rec["earn_date"] = ecal[code]
        rec["earn_src"] = "API"
    rec["earn_bdays"] = days_to_earnings(rec["earn_date"], today)

    frm = (today - dt.timedelta(days=760)).strftime("%Y-%m-%d")
    hist = api(jq, "/equities/bars/daily",
               {"code": code, "from": frm, "to": item["date_target"]})
    hist = [h for h in hist if fnum(h.get("AdjC")) is not None]
    hist.sort(key=lambda h: h.get("Date", ""))
    dates = [h.get("Date", "") for h in hist]
    opens = [fnum(h.get("AdjO")) for h in hist]
    highs = [fnum(h.get("AdjH")) for h in hist]
    lows = [fnum(h.get("AdjL")) for h in hist]
    closes = [fnum(h.get("AdjC")) for h in hist]
    vols = [fnum(h.get("AdjVo")) or 0 for h in hist]
    uls = [str(h.get("UL")) == "1" for h in hist]

    # 押し目の熟成度。調整済み系列から一貫して算出する（S高日の出来高も同系列から採る）
    if item.get("sh_date"):
        dm = dip_metrics(dates, highs, closes, vols, item["sh_date"])
        for k in ("days_from_peak", "dd_pct", "dry_pct", "dry5_pct", "peak", "peak_date"):
            rec[k] = r2(dm[k], 2) if isinstance(dm[k], float) else dm[k]
        if dm["sh_idx"] is None:
            rec["note_data"] = "S高日が日足に無い（調整/配信の齟齬）"
        else:
            rec["sh_vol"] = vols[dm["sh_idx"]]      # 調整済み系列に揃える
    rec["turnover_oku"] = r2(turnover_oku20(closes, vols), 2)

    # 前日比は調整済み系列から出し直す。日付一括の生値 C 同士で割ると、
    # 権利落ち日に「動いていないのに -50%」が出る（本体 kabu-monitor で実害の記録あり）
    if len(closes) >= 2 and closes[-1] is not None and closes[-2]:
        rec["change_pct"] = round((closes[-1] / closes[-2] - 1) * 100, 2)

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

    stmts = api(jq, "/fins/summary", {"code": code})
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
        mgn = api(jq, "/markets/margin-interest", {"code": code})
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


# 旧 label()（初動入口◎/押し目待ち○/監視△）は撤去した。
# 理由: 「買うべき」を機械に判定させないという本プロジェクトの目的と矛盾するため
# （依頼書 G3）。代わりに「S高からN日・高値-X%・枯れ比Y%」という事実列を並べる。


# ----------------------------------------------------------------------
# 出力
# ----------------------------------------------------------------------
# 層Aのテーブル描画（並べ替え・フィルタ）。チャートJS(凍結)とは別スクリプトに分けている。
# f-string ではないので波括弧のエスケープ不要。ROWS/TARGET/NA/MATERIAL_UNSET は上の<script>で定義。
_TABLE_JS = r"""<script>
const COLS = [
  {k:'c',    t:'コード',        num:false},
  {k:'n',    t:'銘柄',          num:false},
  {k:'mk',   t:'市場',          num:false},
  {k:'sh',   t:'ストップ高日',  num:false},
  {k:'dp',   t:'高値から(日)',  num:true},
  {k:'dd',   t:'高値から(%)',   num:true},
  {k:'dry',  t:'枯れ比 当日',   num:true},
  {k:'dry5', t:'枯れ比 5日平均',num:true},
  {k:'to',   t:'代金20日(億)',  num:true},
  {k:'p',    t:'株価',          num:true},
  {k:'ch',   t:'前日比',        num:true},
  {k:'cap',  t:'時価総額(億)',  num:true},
  {k:'ed',   t:'決算まで(営業日)', num:true},
  {k:'mcl',  t:'材料分類',      num:false},
  {k:'tb',   t:'タブー',        num:false}
];
// 既定の並びは Python 側（render の sorted）で確定済みで、ROWS はその順で出力されている。
// ヘッダをクリックするまで JS では並べ替えない（既定順の正しさをテストで固定できるようにするため）。
let sortKey = 'sh', sortDir = -1, userSorted = false;

function fmt(v, suf, nd) {
  if (v === null || v === undefined || v === '') return NA;
  if (typeof v === 'number') return v.toFixed(nd === undefined ? 1 : nd) + (suf || '');
  return v + (suf || '');
}
function cellHtml(col, r) {
  const v = r[col.k];
  const na = (v === null || v === undefined || v === '');
  let txt, cls = col.num ? 'num' : '';
  // タブーは3値: 財務が取れていない=—／該当なし=空欄／該当=理由（—と空欄を取り違えない）
  if (col.k === 'tb') {
    if (r.tbh === null || r.tbh === undefined) return '<td class="na">' + NA + '</td>';
    if (!r.tbh) return '<td></td>';
    return '<td class="warn">' + v + '</td>';
  }
  if (na) { txt = (col.k === 'mcl') ? MATERIAL_UNSET : NA; cls += ' na'; }
  else if (col.k === 'p')   txt = '¥' + fmt(v, '', 1);
  else if (col.k === 'ch')  txt = (v >= 0 ? '+' : '') + fmt(v, '%', 2);
  else if (col.k === 'dd')  txt = fmt(v, '%', 1);
  else if (col.k === 'dry' || col.k === 'dry5') {
    txt = fmt(v, '%', v < 10 ? 1 : 0);   // 0.4% を「0%」と丸めない（—と紛らわしいため）
    if (v <= 30) cls += ' dry-low';
  }
  else if (col.k === 'to' || col.k === 'cap') txt = fmt(v, '', 1);
  else if (col.k === 'dp')  txt = fmt(v, '日', 0);
  else if (col.k === 'ed')  {
    txt = fmt(v, '日', 0) + (r.es ? '(' + r.es + ')' : '');
    if (v <= 5) cls += ' earn-near';
  }
  else if (col.k === 'tb')  { txt = v; cls += ' warn'; }
  else txt = String(v);
  return '<td class="' + cls.trim() + '">' + txt + '</td>';
}
function cmp(a, b) {
  const va = a[sortKey], vb = b[sortKey];
  const na = (va === null || va === undefined || va === '');
  const nb = (vb === null || vb === undefined || vb === '');
  if (na && nb) return 0;
  if (na) return 1;              // 値が無い行は向きによらず最後
  if (nb) return -1;
  let d = (va < vb) ? -1 : (va > vb) ? 1 : 0;
  if (d === 0 && sortKey !== 'dry') {   // 同値は既定の第2キー（枯れ比の低い順）で割る
    const da = a.dry, db = b.dry;
    if (da === null && db === null) return 0;
    if (da === null) return 1;
    if (db === null) return -1;
    return (da < db) ? -1 : (da > db) ? 1 : 0;
  }
  return d * sortDir;
}
function val(id) { const e = document.getElementById(id); return e ? e.value.trim() : ''; }
function num(id) { const v = val(id); return v === '' ? null : parseFloat(v); }
function passes(r) {
  const mkt = val('f-mkt');
  if (mkt && r.mk !== mkt) return false;
  const ddmin = num('f-ddmin'), ddmax = num('f-ddmax');
  if (ddmin !== null || ddmax !== null) {
    if (r.dd === null || r.dd === undefined) return false;
    if (ddmin !== null && r.dd < ddmin) return false;
    if (ddmax !== null && r.dd > ddmax) return false;
  }
  const dry = num('f-dry');
  if (dry !== null) { if (r.dry === null || r.dry === undefined || r.dry > dry) return false; }
  const to = num('f-to');
  if (to !== null) { if (r.to === null || r.to === undefined || r.to < to) return false; }
  const mcl = val('f-mcl');
  if (mcl && r.mcl !== mcl) return false;
  const eb = document.getElementById('f-earn');
  if (eb && eb.checked) {
    const d = num('f-earnd');
    if (d !== null && r.ed !== null && r.ed !== undefined && r.ed <= d) return false;
  }
  const q = val('f-q').toLowerCase();
  if (q && (String(r.c) + ' ' + r.n + ' ' + (r.sec || '')).toLowerCase().indexOf(q) < 0) return false;
  return true;
}
function draw() {
  const th = COLS.map(function (col) {
    const ar = (col.k === sortKey) ? ' <span class="ar">' + (sortDir < 0 ? '▼' : '▲') + '</span>' : '';
    return '<th data-k="' + col.k + '">' + col.t + ar + '</th>';
  }).join('');
  document.getElementById('thead').innerHTML = th;
  COLS.forEach(function (col) {
    const el = document.querySelector('th[data-k="' + col.k + '"]');
    if (el) el.onclick = function () {
      if (userSorted && sortKey === col.k) { sortDir = -sortDir; }
      else { sortKey = col.k; sortDir = col.num ? 1 : -1; }
      userSorted = true;
      draw();
    };
  });
  const rows = userSorted ? ROWS.filter(passes).sort(cmp) : ROWS.filter(passes);
  document.getElementById('tbody').innerHTML = rows.length
    ? rows.map(function (r) {
        return '<tr class="' + (r.sh === TARGET ? 'today' : '') + '" onclick="showChart(\'' + r.rc + '\')">'
             + COLS.map(function (col) { return cellHtml(col, r); }).join('') + '</tr>';
      }).join('')
    : '<tr><td colspan="' + COLS.length + '">該当なし（フィルタを外すと ' + ROWS.length + ' 件）</td></tr>';
  document.getElementById('fcount').textContent = '表示 ' + rows.length + ' / ' + ROWS.length + ' 件';
}
function fillSelect(id, values, allLabel) {
  const el = document.getElementById(id);
  if (!el) return;
  el.innerHTML = '<option value="">' + allLabel + '</option>'
    + values.map(function (v) { return '<option value="' + v + '">' + v + '</option>'; }).join('');
}
(function init() {
  const mkts = [], mcls = [];
  ROWS.forEach(function (r) {
    if (r.mk && mkts.indexOf(r.mk) < 0) mkts.push(r.mk);
    const m = r.mcl || MATERIAL_UNSET;
    if (mcls.indexOf(m) < 0) mcls.push(m);
  });
  mkts.sort(); mcls.sort();
  fillSelect('f-mkt', mkts, 'すべて');
  fillSelect('f-mcl', mcls, 'すべて');
  ['f-mkt', 'f-ddmin', 'f-ddmax', 'f-dry', 'f-to', 'f-mcl', 'f-earn', 'f-earnd', 'f-q']
    .forEach(function (id) {
      const el = document.getElementById(id);
      if (el) { el.addEventListener('input', draw); el.addEventListener('change', draw); }
    });
  const rs = document.getElementById('f-reset');
  if (rs) rs.onclick = function () {
    ['f-ddmin', 'f-ddmax', 'f-dry', 'f-to', 'f-q'].forEach(function (id) {
      const e = document.getElementById(id); if (e) e.value = '';
    });
    ['f-mkt', 'f-mcl'].forEach(function (id) {
      const e = document.getElementById(id); if (e) e.value = '';
    });
    const e = document.getElementById('f-earn'); if (e) e.checked = false;
    draw();
  };
  draw();
})();
</script>"""


def _row_payload(r: Dict[str, Any]) -> Dict[str, Any]:
    """層Aの1行ぶん。キーは短縮名（HTMLの肥大を抑える）。"""
    return {"rc": r["raw_code"], "c": r["code"], "n": r.get("name", ""),
            "mk": r.get("market", ""), "sec": r.get("sector", ""),
            "sh": r.get("sh_date"), "dp": r.get("days_from_peak"),
            "dd": r.get("dd_pct"), "dry": r.get("dry_pct"), "dry5": r.get("dry5_pct"),
            "to": r.get("turnover_oku"), "p": r.get("price"), "ch": r.get("change_pct"),
            "cap": r.get("market_cap_oku"), "ed": r.get("earn_bdays"),
            "es": r.get("earn_src"), "mcl": r.get("material_class", MATERIAL_UNSET),
            "tb": r.get("taboo_reason", ""), "tbh": r.get("taboo_hit")}


def render(target: str, records: List[Dict[str, Any]], crit: Optional[Dict[str, Any]] = None,
           docs: Optional[str] = None, dropped: int = 0):
    crit = crit or DEFAULT_CRITERIA
    docs = docs or DOCS_DIR
    os.makedirs(os.path.join(docs, "data"), exist_ok=True)
    # 既定並び: S高日が新しい順 → 出来高枯れ比が低い順（値が無い行は最後）
    records = sorted(records, key=lambda r: (r.get("sh_date") or "",
                                             -(r.get("dry_pct") if r.get("dry_pct")
                                               is not None else 1e9)), reverse=True)
    # latest.json はチャート系列を持たない（同じ内容が index.html に埋まっているため。
    # 入れると1銘柄あたり約78KB＝90銘柄で7MBになり、毎営業日そのままコミットされる）
    slim = [{k: v for k, v in r.items() if k != "chart"} for r in records]
    payload = {"generated_at": dt.datetime.now(JST).isoformat(timespec="seconds"),
               "data_date": target, "dropped": dropped, "candidates": slim}
    with open(os.path.join(docs, "data", "latest.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    rows_js = json.dumps([_row_payload(r) for r in records], ensure_ascii=False)
    chart_map = {r["raw_code"]: {"name": r["name"], "code": r["code"],
                                 "chart": r.get("chart", [])} for r in records}
    data_js = json.dumps(chart_map, ensure_ascii=False)
    today_sh = sum(1 for r in records if r.get("sh_date") == target)
    gate = bool(crit.get("dip_default_filter"))
    band = list(crit.get("dip_dd_band") or DEFAULT_CRITERIA["dip_dd_band"])
    dry_max = crit.get("dip_dry_max", DEFAULT_CRITERIA["dip_dry_max"])
    # ゲートONのときは実際に入力欄へ値を入れる（注記だけONにして絞らない、をやらない）
    v_ddmin = f' value="{band[0]:g}"' if gate else ""
    v_ddmax = f' value="{band[1]:g}"' if gate else ""
    v_dry = f' value="{dry_max:g}"' if gate else ""
    gate_note = (f"下落率・枯れ比の既定フィルタは <b>ON</b>"
                 f"（下落率 {band[0]:g}〜{band[1]:g}% ／ 枯れ比 ≤{dry_max:g}%。"
                 f"criteria.yaml: dip_default_filter）。「リセット」で全件に戻せる。"
                 if gate else
                 "下落率・枯れ比の既定フィルタは <b>OFF</b>。"
                 "<code>backtest_dip.py</code> が「+5%到達率・平均損益とも無選別ベンチ超え」を"
                 "満たすまで既定では絞らない（B群ゲート）。")
    drop_note = (f" ／ <b>解析失敗 {dropped}件</b>（APIエラー等で一覧から欠落）" if dropped else "")

    html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ストップ高後の押し目 (wbagger-screener)</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
body{{font-family:system-ui,'Hiragino Sans',sans-serif;margin:16px;background:#0d1117;color:#e6edf3}}
h1{{font-size:18px}} .meta{{color:#8b949e;font-size:13px;margin-bottom:12px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border:1px solid #30363d;padding:5px 7px;text-align:left}}
th{{background:#161b22;position:sticky;top:0;cursor:pointer;white-space:nowrap}}
th:hover{{background:#21262d}} th .ar{{color:#58a6ff}}
tbody tr{{cursor:pointer}} tbody tr:hover{{background:#21262d}}
td.num{{text-align:right}} td.warn{{color:#f85149}} td.na{{color:#6e7681}}
tr.today{{background:#13301f}}
.dry-low{{color:#3fb950}} .earn-near{{color:#f0a020}}
#filters{{margin:10px 0;padding:8px 10px;border:1px solid #30363d;border-radius:8px;background:#0f141b;font-size:12px;color:#8b949e}}
#filters label{{margin-right:14px;display:inline-block;line-height:2}}
#filters input,#filters select{{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:2px 5px;font-size:12px}}
#filters input[type=number]{{width:62px}} #filters input[type=text]{{width:130px}}
#fcount{{color:#e6edf3}}
.note{{color:#8b949e;font-size:12px;margin-top:14px;line-height:1.7}}
.note b{{color:#e6edf3}} .note code{{color:#58a6ff}}
#panel{{display:none;margin:16px 0;padding:12px;border:1px solid #30363d;border-radius:8px;background:#0f141b}}
#panel h2{{font-size:15px;margin:.2em 0 .6em}}
#tfbtns{{margin-bottom:8px}}
#tfbtns button{{background:#21262d;color:#e6edf3;border:1px solid #30363d;padding:4px 14px;margin-right:6px;border-radius:6px;cursor:pointer;font-size:12px}}
.cwrap{{position:relative;height:220px;margin-bottom:10px}}
.cwrap.small{{height:140px}}
.close{{float:right;color:#8b949e;cursor:pointer}}
</style></head><body>
<h1>ストップ高後の押し目 — 東証グロース＋スタンダード</h1>
<div class="meta">データ基準日: {target} ／ 生成: {dt.datetime.now(JST).strftime('%Y-%m-%d %H:%M')} JST
 ／ 母集団: 直近{crit.get('sh_window', 20)}営業日にS高 <b>{len(records)}</b>件（うち当日S高 {today_sh}件）{drop_note}
 ／ 行クリックでチャート表示</div>

<div id="panel">
  <span class="close" onclick="document.getElementById('panel').style.display='none'">閉じる ✕</span>
  <h2 id="ptitle"></h2>
  <div id="tfbtns"><button id="btf-d" onclick="setTf('d')">日足</button><button id="btf-w" onclick="setTf('w')">週足</button></div>
  <div class="cwrap"><canvas id="cPrice"></canvas></div>
  <div class="cwrap small"><canvas id="cMacd"></canvas></div>
  <div class="cwrap small"><canvas id="cRci"></canvas></div>
</div>

<div id="filters">
  <label>市場 <select id="f-mkt"></select></label>
  <label>高値からの下落率 <input type="number" id="f-ddmin" step="1" placeholder="下限"{v_ddmin}> 〜 <input type="number" id="f-ddmax" step="1" placeholder="上限"{v_ddmax}> %</label>
  <label>枯れ比 ≤ <input type="number" id="f-dry" step="5" placeholder="%"{v_dry}></label>
  <label>代金 ≥ <input type="number" id="f-to" step="0.5" placeholder="億"></label>
  <label>材料分類 <select id="f-mcl"></select></label>
  <label><input type="checkbox" id="f-earn"> 決算まで <input type="number" id="f-earnd" value="{int(crit.get('earnings_skip_bdays', 5))}" step="1"> 営業日以内を隠す</label>
  <label>検索 <input type="text" id="f-q" placeholder="コード/銘柄"></label>
  <label><button type="button" id="f-reset">リセット</button></label>
  <span id="fcount"></span>
</div>

<table><thead><tr id="thead"></tr></thead><tbody id="tbody"></tbody></table>
<div class="note">
<b>読み方</b>：この表は「買うべき銘柄」を選んだものではない。直近{crit.get('sh_window', 20)}営業日にストップ高をつけた銘柄を全部並べ、
<b>S高からの日数・高値からの下落率・出来高枯れ比</b>で押し目の段階を本人が読み取るためのもの。合成スコア・総合判定は置かない。<br>
<b>出来高枯れ比</b> = 当日出来高 ÷ S高日出来高（%）。5日平均版は 直近5日平均 ÷ S高日。分母0は「{NA}」。
<b>高値からの日数</b> = S高日以降の最高値を最後に付けた日から当日までの営業日数（0=当日が最高値）。
<b>代金</b> = 20日平均売買代金(億円, 当日を含む直近20本の AdjC×AdjVo 平均)。<br>
<b>決算まで</b> = 手入力 <code>manual/&lt;code&gt;.yaml</code> の <code>earnings_date</code> が主。
<code>/equities/earnings-calendar</code> は<b>翌営業日発表分しか返さない限定フィード</b>（date/from/to/code を無視する・実測）なので、
<b>空欄は「発表予定なし」を意味しない</b>。営業日数は土日のみ考慮で祝日未対応（多めに出る）。<br>
{gate_note}
本体 kabu-monitor では「直近で最も上げた順」に並べた指標がのちに逆相関と判明した例（M2≥95 で -2.21%）があり、
<b>枯れ比・下落率にも同じ罠がありうる【推測】</b>。検証を通すまでは表示のみで、抽出条件・並び順の根拠には使わない。<br>
「{NA}」はデータ取得不可・算出不能、「{MATERIAL_UNSET}」は手入力待ち。材料の中身はJ-Quants非配信のためTDnet/EDINETで確認すること。<br>
本表は手法に基づく機械的抽出であり投資助言ではない。最終判断は自己責任。
</div>

<script>
const DATA = {data_js};
const ROWS = {rows_js};
const TARGET = "{target}";
const MATERIAL_UNSET = "{MATERIAL_UNSET}";
const NA = "{NA}";
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
""" + _TABLE_JS + """
</body></html>"""
    with open(os.path.join(docs, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[ok] 出力完了: {os.path.join(docs, 'index.html')}, "
          f"{os.path.join(docs, 'data', 'latest.json')} ({len(records)}件)")


def main() -> int:
    api_key = os.environ.get("JQUANTS_API_KEY")
    if not api_key:
        print("[error] 環境変数 JQUANTS_API_KEY が未設定（v2はAPIキー方式）")
        return 1
    crit = load_criteria()
    jq = JQuants(api_key)
    t0 = time.time()
    cache: Dict[str, List[Dict[str, Any]]] = {}   # 日付一括バーの重複取得を防ぐ

    target = latest_trading_date(jq, cache=cache)
    if not target:
        print("[error] 取引日データが見つかりません(配信状況/APIキーを確認)")
        return 1
    prev = prev_trading_date(jq, target, cache=cache)
    if not prev:
        print("[error] 前営業日が特定できません")
        return 1
    print(f"[ok] 対象日={target} / 前日={prev}")

    uni_codes, names, mkt, sec = market_universe(jq, target, crit["markets"])
    if not uni_codes:
        # ここで止めないと「市場で絞らない」フォールバックが効いて全市場が母集団になり、
        # 見出しと中身が食い違ったまま何時間も走る
        print(f"[error] 対象市場に一致する銘柄が0件です（criteria.yaml の markets={crit['markets']} を確認）")
        return 1
    sh_map = recent_stop_high(jq, uni_codes, target, int(crit["sh_window"]), cache)
    shortlist, chg_all = build_shortlist(jq, target, prev, uni_codes, sh_map, crit, cache)
    ecal = earnings_map(jq, dt.datetime.strptime(target, "%Y-%m-%d").date())
    req_before_codes = REQ["n"]
    # 走査用の日付一括バーは以後使わないので解放（銘柄別ループのメモリを空ける）
    cache.clear()
    for it in shortlist:
        it["date_target"] = target

    records, failed = [], []
    for i, item in enumerate(shortlist, 1):
        try:
            rec = analyze_candidate(jq, item, names, mkt, crit, sec, ecal)
            records.append(rec)
            dd = f"{rec['dd_pct']:+.1f}%" if rec["dd_pct"] is not None else NA
            dry = f"{rec['dry_pct']:.0f}%" if rec["dry_pct"] is not None else NA
            print(f"  [{i}/{len(shortlist)}] {rec['code']} {rec['name']} ({rec['market']}) "
                  f"S高{rec['sh_date']} 高値から{dd} 枯れ比{dry}")
        except Exception as e:
            failed.append(item["code"])
            print(f"  [warn] {item['code']} 解析失敗: {e}")

    # 取りこぼしが多いまま公開すると「候補が少ない日」と区別がつかない。
    # 半分以上落ちたら前回のページを残す（Actions は非0終了でコミット段に進まない）
    if shortlist and len(records) * 2 < len(shortlist):
        print(f"[error] {len(failed)}/{len(shortlist)} 件が解析失敗。"
              f"部分的な結果は公開せず中断します（前回のページを残す）")
        return 1

    render(target, records, crit, dropped=len(failed))
    print(f"[ok] APIリクエスト(論理) 合計 {REQ['n']} 回"
          f"（抽出まで {req_before_codes} / 銘柄別 {REQ['n'] - req_before_codes}）"
          f" / 所要 {time.time() - t0:.0f}秒")
    if failed:
        print(f"[warn] 解析失敗 {len(failed)}件: {', '.join(failed[:20])}"
              f"{' ほか' if len(failed) > 20 else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
