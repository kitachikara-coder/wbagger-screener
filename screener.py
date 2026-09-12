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


# ----------------------------------------------------------------------
# 層B（銘柄カード）— チャートで見えない情報
# ----------------------------------------------------------------------
def parse_margin(rows: List[Dict[str, Any]], weeks: int = 3) -> List[Dict[str, Any]]:
    """信用取引週末残高(週次)の直近 weeks 週。Date は金曜。

    信用倍率 = LongVol ÷ ShrtVol。**日証金の貸借倍率とは別物**（あちらは非配信）。
    ShrtVol が 0 / 欠損の週は倍率だけ None にして、週そのものは残す
    （「売残ゼロ」は消してよい情報ではないため）。
    同一 Date に IssType 違いの行が来た場合は最後の1行を採る。
    """
    by_date: Dict[str, Dict[str, Any]] = {}
    for r in rows or []:
        d = str(r.get("Date") or "").strip()
        if d:
            by_date[d] = r
    out: List[Dict[str, Any]] = []
    for d in sorted(by_date)[-max(1, weeks):]:
        r = by_date[d]
        lv, sv = fnum(r.get("LongVol")), fnum(r.get("ShrtVol"))
        neg = fnum(r.get("LongNegVol"))
        out.append({"date": d, "long": lv, "short": sv,
                    "ratio": (lv / sv) if (lv is not None and sv) else None,
                    "std_ratio": ((fnum(r.get("LongStdVol")) / fnum(r.get("ShrtStdVol")))
                                  if (fnum(r.get("LongStdVol")) is not None
                                      and fnum(r.get("ShrtStdVol"))) else None),
                    "neg_pct": (neg / lv * 100) if (neg is not None and lv) else None,
                    "d_long": None, "d_ratio": None})
    for i in range(1, len(out)):
        p, c = out[i - 1], out[i]
        if c["long"] is not None and p["long"] is not None:
            c["d_long"] = c["long"] - p["long"]
        if c["ratio"] is not None and p["ratio"] is not None:
            c["d_ratio"] = c["ratio"] - p["ratio"]
    return out


def fetch_margin(jq: JQuants, code: str, target: Optional[str] = None,
                 weeks: int = 3) -> Tuple[List[Dict[str, Any]], str]:
    """(直近weeks週, 取得できなかった理由) を返す。取得できたら理由は空文字。

    403/401 は「プラン外」、それ以外の HTTP エラーと通信断は「取得失敗」、
    200 でも中身が空なら「データ無し」と、**理由を区別して**返す
    （画面で「—」の意味が分かるようにするため）。
    """
    params: Dict[str, Any] = {"code": code}
    if target:
        frm = (dt.datetime.strptime(target, "%Y-%m-%d").date()
               - dt.timedelta(days=30 * (weeks + 2))).strftime("%Y-%m-%d")
        params.update({"from": frm, "to": target})
    try:
        rows = api(jq, "/markets/margin-interest", params)
    except requests.HTTPError as e:
        st = getattr(getattr(e, "response", None), "status_code", None)
        if st in (401, 403):
            return [], "プラン外（Standard以上が必要）"
        return [], f"取得失敗(HTTP {st})"
    except Exception as e:
        return [], f"取得失敗({type(e).__name__})"
    if not rows:
        return [], "データ無し"
    parsed = parse_margin(rows, weeks)
    return (parsed, "" if parsed else "データ無し")


def volume_profile(dates: List[str], vols: List[Optional[float]],
                   sh_idx: Optional[int], n: int = 10) -> List[Dict[str, Any]]:
    """S高日=100 とした直近 n 営業日の出来高。S高日が窓の外でも基準は S高日のまま。"""
    if sh_idx is None or not dates:
        return []
    base = vols[sh_idx] if sh_idx < len(vols) else None
    out = []
    for i in range(max(0, len(dates) - n), len(dates)):
        v = vols[i] if i < len(vols) else None
        out.append({"d": dates[i][5:].replace("-", "/"),
                    "pct": (v / base * 100) if (base and v is not None) else None,
                    "sh": i == sh_idx})
    return out


def dip_volume_split(closes: List[Optional[float]], vols: List[Optional[float]],
                     sh_idx: Optional[int]) -> Dict[str, Any]:
    """S高日の翌日以降を、前日比マイナスの日とプラスの日に分けた平均出来高。

    「下げる日ほど商いが細っているか」を数字で見るための素材。解釈は載せない。
    """
    out = {"down_avg": None, "up_avg": None, "down_n": 0, "up_n": 0}
    if sh_idx is None:
        return out
    down, up = [], []
    for i in range(max(1, sh_idx + 1), min(len(closes), len(vols))):
        c, p, v = closes[i], closes[i - 1], vols[i]
        if c is None or p is None or v is None:
            continue
        (up if c >= p else down).append(v)
    if down:
        out["down_avg"], out["down_n"] = sum(down) / len(down), len(down)
    if up:
        out["up_avg"], out["up_n"] = sum(up) / len(up), len(up)
    return out


def position_summary(closes: List[Optional[float]], dd_pct: Optional[float]) -> str:
    """「5MA下・25MA上・75MA上・高値-14.2%」の1行。チャートで見える内容の要約なので短く。"""
    vals = [c for c in closes if c is not None]
    if not vals:
        return NA
    price, parts = vals[-1], []
    for n in (5, 25, 75):
        m = sma(vals, n)
        parts.append(f"{n}MA{'上' if price > m else '下'}" if m is not None else f"{n}MA{NA}")
    parts.append(f"高値{dd_pct:+.1f}%" if dd_pct is not None else f"高値{NA}")
    return "・".join(parts)


def sector_comove(code: str, sec: Dict[str, str], chg_all: Dict[str, float],
                  thr: float = 3.0) -> Dict[str, Any]:
    """同一33業種のうち、当日 前日比 ≥ thr% だった銘柄数（母数つき）。追加API 0。

    母数は全市場（グロース＋スタンダードに限らない）。業種の広がりを見るため。
    """
    name = sec.get(code, "")
    out = {"name": name, "hot": None, "total": None, "thr": thr}
    if not name:
        return out
    peers = [c for c, s in sec.items() if s == name and c in chg_all]
    if not peers:
        return out
    out["total"] = len(peers)
    out["hot"] = sum(1 for c in peers if chg_all[c] >= thr)
    return out


def past_sh_episodes(dates: List[str], highs: List[Optional[float]],
                     closes: List[Optional[float]], uls: List[bool],
                     gap: int = 20, fwd: int = 5, limit: int = 6) -> List[Dict[str, Any]]:
    """同一銘柄の過去のS高エピソード（S高日 → 最大押し → その後5営業日の騰落）。

    **参考表示のみ。予測には使わない。** 直近 gap 本に入るエピソード（＝今回の分）は
    まだ結果が出ていないので除く。連続したS高は gap 本以内なら1エピソードにまとめる。
    """
    n = len(dates)
    eps: List[Dict[str, Any]] = []
    last = None
    for i in range(n):
        if not uls[i]:
            continue
        if last is not None and i - last < gap:
            last = i
            continue
        last = i
        if i >= n - gap:                 # 進行中のエピソードは結果が確定していない
            continue
        hs = [h for h in highs[i:i + gap + 1] if h is not None]
        cs = [c for c in closes[i:i + gap + 1] if c is not None]
        peak = max(hs) if hs else None
        trough = min(cs) if cs else None
        c0 = closes[i]
        cf = closes[i + fwd] if i + fwd < n else None
        eps.append({"d": dates[i],
                    "dd": ((trough / peak - 1) * 100) if (peak and trough) else None,
                    "r5": ((cf / c0 - 1) * 100) if (c0 and cf) else None})
    return eps[-limit:]


# ----------------------------------------------------------------------
# P3: 判断ログ（買う / 見送り を自分で記録し、5営業日後の実績と突き合わせる）
# ----------------------------------------------------------------------
DECISIONS_REL = os.path.join("data", "decisions.json")
DECISION_HOLD = 5          # 突合する営業日数
DECISION_MAX_FETCH = 30    # 当日の候補に居ないコードを追加取得する上限（超えた分は理由を出す）


def load_decisions(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """docs/data/decisions.json を読む。無い・空・壊れていても [] を返して止めない。

    1件ずつ検証し、date と code が読めない行だけを捨てる（1行の書き損じで全部を失わない）。
    """
    path = path or os.path.join(DOCS_DIR, DECISIONS_REL)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"[warn] {path} が読めません({e})。判断ログは空として続行します")
        return []
    items = raw.get("decisions") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        print(f"[warn] {path} の decisions が配列ではありません。判断ログは空として続行します")
        return []
    out, bad = [], 0
    for r in items:
        if not isinstance(r, dict):
            bad += 1
            continue
        d, c = _norm_date(r.get("date")), str(r.get("code") or "").strip()
        if d is None or not c:
            bad += 1
            continue
        out.append({"date": d.isoformat(), "code": disp_code(c),
                    "action": str(r.get("action") or "").strip() or "buy",
                    "reason": str(r.get("reason") or "").strip(),
                    "price": fnum(r.get("price"))})
    if bad:
        print(f"[warn] 判断ログの {bad}件は date/code が読めないため無視しました")
    out.sort(key=lambda r: (r["date"], r["code"]))
    return out


def settle_decision(dec: Dict[str, Any], dates: List[str], closes: List[Optional[float]],
                    today: str, hold: int = DECISION_HOLD) -> Dict[str, Any]:
    """判断日の hold 営業日後の終値と price を比べる。日足は昇順・調整済みを渡すこと。

    - 判断日が休場なら「その日以降で最初の営業日」を起点にする
    - hold 営業日ぶんのバーがまだ無ければ「経過待ち」（残り日数つき）
    - price が無ければ起点日の終値を建値とみなす（見送りの記録で price を省けるように）
    """
    out = {"status": "unknown", "base_date": None, "entry": None,
           "exit_date": None, "exit": None, "pnl_pct": None, "left": None, "reason": ""}
    if not dates:
        out["reason"] = "日足なし"
        return out
    i = next((k for k, d in enumerate(dates) if d >= dec["date"]), None)
    if i is None:
        out["reason"] = "判断日が日足の範囲外"
        return out
    entry = dec.get("price")
    if entry is None:
        entry = closes[i] if i < len(closes) else None
    if entry is None or entry <= 0:
        out["reason"] = "建値が不明"
        return out
    out["base_date"], out["entry"] = dates[i], entry
    j = i + hold
    if j >= len(dates):
        # まだ hold 営業日ぶんのバーが無い（today より先の話）
        out["status"] = "pending"
        out["left"] = hold - (len(dates) - 1 - i)
        return out
    c = closes[j]
    if c is None:
        out["reason"] = "手仕舞い日の終値なし"
        return out
    out["status"] = "done"
    out["exit_date"], out["exit"] = dates[j], c
    out["pnl_pct"] = (c / entry - 1) * 100
    return out


def settle_decisions(jq: JQuants, decisions: List[Dict[str, Any]],
                     series_by_code: Dict[str, Tuple[List[str], List[Optional[float]]]],
                     target: str, max_fetch: int = DECISION_MAX_FETCH) -> List[Dict[str, Any]]:
    """判断ログに実績を付ける。当日の候補に無いコードだけ日足を追加取得する。

    追加取得は max_fetch 件まで。打ち切った分は結果に理由を残す（黙って落とさない）。
    """
    need = sorted({d["code"] for d in decisions if d["code"] not in series_by_code})
    fetched, skipped = 0, []
    frm = (dt.datetime.strptime(target, "%Y-%m-%d").date()
           - dt.timedelta(days=400)).strftime("%Y-%m-%d")
    for code in need:
        if fetched >= max_fetch:
            skipped.append(code)
            continue
        try:
            rows = api(jq, "/equities/bars/daily", {"code": code, "from": frm, "to": target})
        except Exception as e:
            print(f"  [warn] 判断ログ {code} の日足取得に失敗: {e}")
            continue
        rows = [r for r in rows if fnum(r.get("AdjC")) is not None]
        rows.sort(key=lambda r: r.get("Date", ""))
        series_by_code[code] = ([r.get("Date", "") for r in rows],
                                [fnum(r.get("AdjC")) for r in rows])
        fetched += 1
    if skipped:
        print(f"[warn] 判断ログの {len(skipped)}件は追加取得の上限({max_fetch})を超えたため"
              f"実績を出せません: {', '.join(skipped)}")
    out = []
    for d in decisions:
        dates, closes = series_by_code.get(d["code"], ([], []))
        r = dict(d)
        if not dates and d["code"] in skipped:
            r["outcome"] = {"status": "unknown", "reason": f"追加取得の上限({max_fetch})超過",
                            "base_date": None, "entry": None, "exit_date": None,
                            "exit": None, "pnl_pct": None, "left": None}
        else:
            r["outcome"] = settle_decision(d, dates, closes, target)
            if isinstance(r["outcome"].get("pnl_pct"), float):
                r["outcome"]["pnl_pct"] = round(r["outcome"]["pnl_pct"], 2)
        out.append(r)
    if decisions:
        done = sum(1 for r in out if r["outcome"]["status"] == "done")
        pend = sum(1 for r in out if r["outcome"]["status"] == "pending")
        print(f"[ok] 判断ログ {len(out)}件（実績確定 {done} / 経過待ち {pend} / "
              f"不明 {len(out) - done - pend}）・追加取得 {fetched}回")
    return out


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
                      chg_all: Optional[Dict[str, float]] = None,
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
        "note_data": "", "chart": [], "card": {},
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
    dm = {"sh_idx": None}
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

    # 信用取引週末残高（G2 修正: v2 の実名は LongVol / ShrtVol）
    mweeks, mreason = fetch_margin(jq, code, item["date_target"])
    if mweeks and mweeks[-1]["long"] is not None:
        rec["margin_long_k"] = round(mweeks[-1]["long"] / 1000, 1)

    # 買残÷浮動株（浮動株はJ-Quants非配信。手入力がある銘柄だけ）
    float_pct = None
    try:
        fs = manual.get("float_shares")
        fs = float(fs) if fs not in (None, "") else None
    except (TypeError, ValueError):
        fs = None
    if fs and fs > 0 and mweeks and mweeks[-1]["long"] is not None:
        float_pct = mweeks[-1]["long"] / fs * 100

    sh_idx = dm["sh_idx"] if item.get("sh_date") else None
    rec["card"] = {
        "material": str(manual.get("material") or "").strip(),
        "material_class": rec["material_class"],
        "continuity": str(manual.get("continuity") or "").strip(),
        "note": str(manual.get("note") or "").strip(),
        "earn": {"date": rec["earn_date"], "src": rec["earn_src"], "bdays": rec["earn_bdays"]},
        "margin": {"weeks": [{k: (r2(v, 3) if isinstance(v, float) else v)
                              for k, v in w.items()} for w in mweeks],
                   "reason": mreason, "float_pct": r2(float_pct, 1)},
        "vol_profile": [{"d": p["d"], "pct": r2(p["pct"], 1), "sh": p["sh"]}
                        for p in volume_profile(dates, vols, sh_idx)],
        "vol_split": {k: (r2(v, 0) if isinstance(v, float) else v)
                      for k, v in dip_volume_split(closes, vols, sh_idx).items()},
        "pos": position_summary(closes, rec["dd_pct"]),
        "sector": sector_comove(code, sec or {}, chg_all or {}),
        "episodes": [{"d": e["d"], "dd": r2(e["dd"], 1), "r5": r2(e["r5"], 1)}
                     for e in past_sh_episodes(dates, highs, closes, uls)],
        "funda": {"eqar": rec["equity_ratio"], "opm": rec["op_margin"], "roe": rec["roe"],
                  "trend": rec["profit_trend"], "cap": rec["market_cap_oku"],
                  "volx": rec["volume_x"], "po": rec["ma_perfect_order"],
                  "macd": rec["macd_cross"], "stop_loss": rec["stop_loss"],
                  "turnover": rec["turnover_oku"]},
        "taboo": rec["taboo_reason"], "taboo_hit": rec["taboo_hit"],
        "sh_date": rec["sh_date"], "sh_vol": rec["sh_vol"],
    }
    rec["_series"] = (dates, closes)     # 判断ログの突合で使い回す（追加API 0）。latest.json には出さない
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
    return '<td class="warn">' + esc(v) + '</td>';
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
  else txt = esc(v);
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
// ---- 層B: 銘柄カード（チャートで見えない情報だけ） --------------------------
let vchart = null;
function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function nv(v, nd, suf) {
  if (v === null || v === undefined || v === '') return '<span class="muted">' + NA + '</span>';
  return (typeof v === 'number' ? v.toFixed(nd === undefined ? 1 : nd) : esc(v)) + (suf || '');
}
function sv(v, nd, suf) {   // 符号つき
  if (v === null || v === undefined) return '<span class="muted">' + NA + '</span>';
  return (v >= 0 ? '+' : '') + v.toFixed(nd === undefined ? 1 : nd) + (suf || '');
}
function kv(k, v, cls) { return '<div class="kv"><span>' + k + '</span><b class="' + (cls || '') + '">' + v + '</b></div>'; }
function ku(n) {   // 株数を千株単位で
  if (n === null || n === undefined) return '<span class="muted">' + NA + '</span>';
  return (n / 1000).toFixed(0) + '千株';
}
function secMaterial(c) {
  let h = '<section><h3>材料（手入力）</h3>';
  h += kv('分類', c.material_class && c.material_class !== MATERIAL_UNSET
          ? esc(c.material_class) : '<span class="muted">' + MATERIAL_UNSET + '</span>');
  h += c.material ? '<div class="body">' + esc(c.material) + '</div>'
                  : '<div class="body muted">未入力（manual/&lt;code&gt;.yaml に material を書く）</div>';
  if (c.continuity) h += '<div class="body">継続性: ' + esc(c.continuity) + '</div>';
  if (c.note) h += '<div class="body muted">' + esc(c.note) + '</div>';
  h += '<div class="ref">材料の中身はJ-Quants非配信。TDnet/EDINET で確認して手入力する。</div>';
  return h + '</section>';
}
function secPosition(c) {
  const f = c.funda || {};
  let h = '<section><h3>位置とファンダ（一覧から移動）</h3>';
  h += kv('位置', esc(c.pos));
  h += kv('決算日', c.earn && c.earn.date
          ? esc(c.earn.date) + '（' + esc(c.earn.src) + '）あと ' + nv(c.earn.bdays, 0, '営業日')
          : '<span class="muted">' + NA + '</span>');
  h += kv('自己資本比率', nv(f.eqar, 1, '%'), (f.eqar !== null && f.eqar <= 30) ? 'warn' : '');
  h += kv('営業利益率', f.opm === null || f.opm === undefined
          ? '<span class="muted">' + NA + '</span>' : (f.opm * 100).toFixed(1) + '%');
  h += kv('ROE', nv(f.roe, 1, '%'));
  h += kv('増益', nv(f.trend, 0));
  h += kv('時価総額', nv(f.cap, 1, '億'));
  h += kv('出来高倍(20日平均比)', nv(f.volx, 1, '倍'));
  h += kv('P.O. / MACD', (f.po === true ? '○' : f.po === false ? '×' : NA) + ' / '
          + (f.macd === true ? '○' : f.macd === false ? '×' : NA));
  h += kv('逆指値目安(直近5日安値)', nv(f.stop_loss, 0));
  if (c.taboo_hit) h += kv('タブー', esc(c.taboo), 'warn');
  return h + '</section>';
}
function secMargin(c) {
  const m = c.margin || {}, ws = m.weeks || [];
  let h = '<section><h3>信用残（週次・金曜時点）</h3>';
  if (!ws.length) {
    h += '<div class="body muted">' + NA + '（' + esc(m.reason || '取得不可') + '）</div>';
  } else {
    h += '<table><thead><tr><th>週</th><th>買残</th><th>売残</th><th>倍率</th><th>買残前週比</th></tr></thead><tbody>';
    ws.forEach(function (w) {
      h += '<tr><td>' + esc(w.date) + '</td><td>' + ku(w.long) + '</td><td>' + ku(w.short)
         + '</td><td>' + nv(w.ratio, 2, '倍') + '</td><td>'
         + (w.d_long === null || w.d_long === undefined ? '<span class="muted">' + NA + '</span>'
            : (w.d_long >= 0 ? '↑' : '↓') + ku(Math.abs(w.d_long))) + '</td></tr>';
    });
    h += '</tbody></table>';
    const last = ws[ws.length - 1];
    h += kv('信用倍率 前週比', sv(last.d_ratio, 2, '倍'));
    if (m.float_pct !== null && m.float_pct !== undefined) {
      h += kv('買残÷浮動株', m.float_pct.toFixed(1) + '%',
              m.float_pct >= 25 ? 'warn' : m.float_pct >= 10 ? 'warn' : '');
    }
  }
  h += '<div class="ref">信用倍率＝買残÷売残。<b>日証金の貸借倍率とは別物</b>（日証金分はJ-Quants非配信）。'
     + '買残÷浮動株は浮動株を手入力した銘柄だけ（10%超で警告色）。'
     + '週次データは基準日の翌週に公表されるので、当日の需給ではない。</div>';
  return h + '</section>';
}
function secVolume(c) {
  const vs = c.vol_split || {};
  let h = '<section><h3>出来高（S高日=100）</h3><div class="vwrap"><canvas id="cVol"></canvas></div>';
  h += kv('下落日の平均出来高', ku(vs.down_avg) + '（' + vs.down_n + '日）');
  h += kv('反発日の平均出来高', ku(vs.up_avg) + '（' + vs.up_n + '日）');
  h += '<div class="ref">S高日の翌日以降を前日比の符号で分けただけの実測値。'
     + '「枯れている＝買い」ではない。B群の検証を通すまで判断材料の一つとして見る。</div>';
  return h + '</section>';
}
function secSector(c) {
  const s = c.sector || {};
  let h = '<section><h3>セクター連動・過去の類似局面</h3>';
  h += kv('33業種', s.name ? esc(s.name) : '<span class="muted">' + NA + '</span>');
  h += kv('同業種で当日 +' + (s.thr || 3) + '%以上',
          (s.hot === null || s.hot === undefined) ? '<span class="muted">' + NA + '</span>'
          : s.hot + ' / ' + s.total + '銘柄');
  const eps = c.episodes || [];
  if (eps.length) {
    h += '<table><thead><tr><th>過去のS高日</th><th>その後の最大押し</th><th>5営業日後</th></tr></thead><tbody>';
    eps.forEach(function (e) {
      h += '<tr><td>' + esc(e.d) + '</td><td>' + nv(e.dd, 1, '%') + '</td><td>'
         + sv(e.r5, 1, '%') + '</td></tr>';
    });
    h += '</tbody></table>';
  } else {
    h += '<div class="body muted">過去2年に完了したS高エピソードなし</div>';
  }
  h += '<div class="ref"><b>参考のみ・予測に使わない。</b>同一銘柄の過去2年のS高について、'
     + 'S高日から20営業日以内の終値最安値（対 期間最高値）と5営業日後の騰落を並べただけ。'
     + '件数が少なく、地合いも違う。同業種の本数も母数が業種ごとに違う。</div>';
  return h + '</section>';
}
// ---- P3: 判断ログ ----------------------------------------------------------
function pnlHtml(o) {
  if (!o) return '<span class="muted">' + NA + '</span>';
  if (o.status === 'pending') return '<span class="muted">経過待ち（あと' + o.left + '営業日）</span>';
  if (o.status !== 'done') return '<span class="muted">' + NA + '（' + esc(o.reason || '不明') + '）</span>';
  const cls = o.pnl_pct >= 0 ? 'good' : 'warn';
  return '<b class="' + cls + '">' + sv(o.pnl_pct, 2, '%') + '</b>'
       + ' <span class="muted">(' + esc(o.base_date) + ' ' + o.entry.toFixed(0)
       + ' → ' + esc(o.exit_date) + ' ' + o.exit.toFixed(0) + ')</span>';
}
function decRows(list) {
  return list.map(function (d) {
    return '<tr><td>' + esc(d.date) + '</td><td>' + esc(d.code) + '</td><td>'
         + (d.action === 'buy' ? '買う' : d.action === 'skip' ? '見送り' : esc(d.action))
         + '</td><td>' + esc(d.reason) + '</td><td>' + pnlHtml(d.outcome) + '</td></tr>';
  }).join('');
}
function secDecision(code, disp) {
  const mine = DECISIONS.filter(function (d) { return d.code === disp; });
  let h = '<section><h3>判断ログ</h3>';
  h += '<div class="kv"><span>判断</span><b>'
     + '<label><input type="radio" name="dact" value="buy" checked> 買う</label> '
     + '<label><input type="radio" name="dact" value="skip"> 見送り</label></b></div>';
  h += '<div style="margin:6px 0"><input type="text" id="d-reason" placeholder="理由（例: 25MA到達＋枯れ比18%で反発）" style="width:100%"></div>';
  h += '<div style="margin:6px 0"><label>建値 <input type="number" id="d-price" step="1" style="width:90px"></label> '
     + '<button type="button" id="d-copy" data-code="' + esc(disp) + '">JSON行をコピー</button> '
     + '<span id="d-msg" class="muted"></span></div>';
  if (mine.length) {
    h += '<table><thead><tr><th>判断日</th><th>コード</th><th>判断</th><th>理由</th><th>'
       + DECISION_HOLD + '営業日後</th></tr></thead><tbody>' + decRows(mine) + '</tbody></table>';
  } else {
    h += '<div class="body muted">この銘柄の記録はまだ無い</div>';
  }
  h += '<div class="ref">コピーした1行を GitHub 上で <code>docs/data/decisions.json</code> の '
     + '<code>decisions</code> 配列に貼って Commit する（Pages から直接は書き込めない）。'
     + '次回の実行で' + DECISION_HOLD + '営業日後の終値と突き合わせて損益が入る。'
     + '建値を空にすると判断日の終値を建値とみなす。</div>';
  return h + '</section>';
}
function wireDecision() {
  const btn = document.getElementById('d-copy');
  if (!btn) return;
  btn.onclick = function () {
    const act = document.querySelector('input[name="dact"]:checked');
    const price = document.getElementById('d-price').value.trim();
    const row = {date: TARGET, code: btn.getAttribute('data-code'),
                 action: act ? act.value : 'buy',
                 reason: document.getElementById('d-reason').value.trim()};
    if (price !== '') row.price = parseFloat(price);
    const text = JSON.stringify(row) + ',';
    const msg = document.getElementById('d-msg');
    const done = function (ok) { msg.textContent = ok ? 'コピーした: ' + text : text; };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { done(true); },
                                               function () { done(false); });
    } else { done(false); }
  };
}

function renderCard(code) {
  const el = document.getElementById('card');
  if (!el) return;
  if (vchart) { vchart.destroy(); vchart = null; }
  const d = DATA[code], c = d && d.card;
  if (!c || !Object.keys(c).length) { el.innerHTML = ''; return; }
  el.innerHTML = secMaterial(c) + secPosition(c) + secMargin(c) + secVolume(c) + secSector(c)
               + secDecision(code, d.code);
  wireDecision();
  const vp = c.vol_profile || [];
  const cv = document.getElementById('cVol');
  if (cv && vp.length) {
    vchart = new Chart(cv, {type: 'bar', data: {labels: vp.map(function (p) { return p.d; }),
      datasets: [{label: 'S高日=100', data: vp.map(function (p) { return p.pct; }),
        backgroundColor: vp.map(function (p) { return p.sh ? '#f0a020' : '#39506b'; })}]},
      options: {responsive: true, maintainAspectRatio: false,
        plugins: {legend: {display: false}},
        scales: {x: {grid: {color: '#222'}, ticks: {color: '#8b949e', font: {size: 9}, maxTicksLimit: 10}},
                 y: {grid: {color: '#222'}, ticks: {color: '#8b949e', font: {size: 9}}}}}});
  }
}
function openRow(code) { showChart(code); renderCard(code); }

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
        return '<tr class="' + (r.sh === TARGET ? 'today' : '') + '" onclick="openRow(\'' + r.rc + '\')">'
             + COLS.map(function (col) { return cellHtml(col, r); }).join('') + '</tr>';
      }).join('')
    : '<tr><td colspan="' + COLS.length + '">該当なし（フィルタを外すと ' + ROWS.length + ' 件）</td></tr>';
  document.getElementById('fcount').textContent = '表示 ' + rows.length + ' / ' + ROWS.length + ' 件';
}
function fillSelect(id, values, allLabel) {
  const el = document.getElementById(id);
  if (!el) return;
  el.innerHTML = '<option value="">' + allLabel + '</option>'
    + values.map(function (v) { return '<option value="' + esc(v) + '">' + esc(v) + '</option>'; }).join('');
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
  const dl = document.getElementById('declog');
  if (dl) {
    dl.innerHTML = DECISIONS.length
      ? '<table><thead><tr><th>判断日</th><th>コード</th><th>判断</th><th>理由</th><th>'
        + DECISION_HOLD + '営業日後</th></tr></thead><tbody>'
        + decRows(DECISIONS.slice().reverse()) + '</tbody></table>'
      : '<div class="muted">まだ1件も記録が無い。'
        + '行をクリックしてカードの「判断ログ」から1行コピーし、'
        + 'GitHub 上で docs/data/decisions.json に貼る。</div>';
  }
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


def _json_script(obj: Any) -> str:
    """<script> の中に置くJSON。`</script>` でスクリプトが閉じないようエスケープする。

    社名はAPI由来だが、材料メモは手入力の自由記述なので実際に起こりうる。
    JSON としては \\u003c 等がそのまま元の文字に戻るので、意味は変わらない。
    """
    return (json.dumps(obj, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


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
           docs: Optional[str] = None, dropped: int = 0,
           decisions: Optional[List[Dict[str, Any]]] = None):
    crit = crit or DEFAULT_CRITERIA
    docs = docs or DOCS_DIR
    os.makedirs(os.path.join(docs, "data"), exist_ok=True)
    # 既定並び: S高日が新しい順 → 出来高枯れ比が低い順（値が無い行は最後）
    records = sorted(records, key=lambda r: (r.get("sh_date") or "",
                                             -(r.get("dry_pct") if r.get("dry_pct")
                                               is not None else 1e9)), reverse=True)
    # latest.json はチャート系列を持たない（同じ内容が index.html に埋まっているため。
    # 入れると1銘柄あたり約78KB＝90銘柄で7MBになり、毎営業日そのままコミットされる）
    decisions = decisions or []
    slim = [{k: v for k, v in r.items() if k not in ("chart", "_series")} for r in records]
    payload = {"generated_at": dt.datetime.now(JST).isoformat(timespec="seconds"),
               "data_date": target, "dropped": dropped, "candidates": slim,
               "decisions": decisions}
    with open(os.path.join(docs, "data", "latest.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    rows_js = _json_script([_row_payload(r) for r in records])
    dec_js = _json_script(decisions)
    chart_map = {r["raw_code"]: {"name": r["name"], "code": r["code"],
                                 "chart": r.get("chart", []),
                                 "card": r.get("card", {})} for r in records}
    data_js = _json_script(chart_map)
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
#card{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px;margin-bottom:12px;font-size:12px}}
#card section{{border:1px solid #30363d;border-radius:6px;padding:8px 10px;background:#0d1117;min-width:0}}
#card h3{{font-size:12px;margin:0 0 6px;color:#8b949e;font-weight:600}}
#card .kv{{display:flex;justify-content:space-between;gap:8px;line-height:1.8;border-bottom:1px dotted #21262d}}
#card .kv:last-child{{border-bottom:0}} #card .kv b{{font-weight:600}}
#card .muted{{color:#6e7681}} #card .warn{{color:#f85149}} #card .good{{color:#3fb950}}
#card .body{{white-space:pre-wrap;line-height:1.7}}
#card table{{font-size:11px;width:100%}} #card th,#card td{{padding:2px 4px}}
#card .ref{{color:#6e7681;font-size:11px;margin-top:6px;line-height:1.6}}
#card .vwrap{{position:relative;height:110px}}
#card input[type=text],#card input[type=number]{{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:2px 5px;font-size:12px}}
#card button{{background:#21262d;color:#e6edf3;border:1px solid #30363d;padding:3px 10px;border-radius:5px;cursor:pointer;font-size:12px}}
h2{{font-size:15px;margin:22px 0 8px}}
#declog{{font-size:12px;color:#8b949e}} #declog table{{width:auto;min-width:min(100%,700px)}}
#declog .muted{{color:#6e7681}} #declog .warn{{color:#f85149}} #declog .good{{color:#3fb950}}
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
  <div id="card"></div>
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

<h2 id="declog-h">判断ログ（買う／見送りの記録と {DECISION_HOLD}営業日後の実績）</h2>
<div id="declog"></div>

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
<b>行をクリック</b>すると、チャートの上に銘柄カードが開く。カードにはチャートで見えない情報だけを置いた
（材料・材料分類・継続性＝手入力／決算日／信用買残3週と信用倍率／買残÷浮動株／S高日=100の出来高推移／
下落日と反発日の平均出来高／位置要約／同業種の当日上昇本数／過去2年のS高エピソード）。
<b>信用倍率＝買残÷売残で、日証金の貸借倍率とは別物</b>（日証金分はJ-Quants非配信）。<br>
「{NA}」はデータ取得不可・算出不能、「{MATERIAL_UNSET}」は手入力待ち。材料の中身はJ-Quants非配信のためTDnet/EDINETで確認すること。<br>
<b>判断ログ</b>は自分で書いた記録で、システムの推奨ではない。{DECISION_HOLD}営業日後の終値との差を機械的に出すだけで、
「当たり/外れ」の判定でも次の売買の根拠でもない。件数が貯まるまでは方向すら読めない【推測】。<br>
本表は手法に基づく機械的抽出であり投資助言ではない。最終判断は自己責任。
</div>

<script>
const DATA = {data_js};
const ROWS = {rows_js};
const TARGET = "{target}";
const MATERIAL_UNSET = "{MATERIAL_UNSET}";
const NA = "{NA}";
const DECISIONS = {dec_js};
const DECISION_HOLD = {DECISION_HOLD};
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
            rec = analyze_candidate(jq, item, names, mkt, crit, sec, ecal, chg_all)
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

    series_by_code = {r["code"]: r.pop("_series") for r in records if "_series" in r}
    decisions = settle_decisions(jq, load_decisions(), series_by_code, target)

    render(target, records, crit, dropped=len(failed), decisions=decisions)
    print(f"[ok] APIリクエスト(論理) 合計 {REQ['n']} 回"
          f"（抽出まで {req_before_codes} / 銘柄別 {REQ['n'] - req_before_codes}）"
          f" / 所要 {time.time() - t0:.0f}秒")
    if failed:
        print(f"[warn] 解析失敗 {len(failed)}件: {', '.join(failed[:20])}"
              f"{' ほか' if len(failed) > 20 else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
