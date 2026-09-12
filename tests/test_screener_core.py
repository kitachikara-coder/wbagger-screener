#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""screener.py の層A（ふるい一覧）ロジックの単体テスト。ネット不要。

`python3 -m pytest tests/ -q` でも `python3 tests/test_screener_core.py` でも実行可。
依頼書 V2 の 1/2/3/5/6/8 に対応（4=fetch_margin は P2、7=decisions.json は P3）。
"""

import os
import sys
import json
import shutil
import datetime as dt
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import screener as sc   # noqa: E402


# ----------------------------------------------------------------------
# 共通モック
# ----------------------------------------------------------------------
class FakeJQ:
    """JQuants.get だけを模した最小モック（本体は凍結なので継承しない）。"""

    def __init__(self, by_date=None, by_path=None):
        self.by_date = by_date or {}
        self.by_path = by_path or {}
        self.calls = []

    def get(self, path, params=None):
        p = dict(params or {})
        self.calls.append((path, p))
        if path == "/equities/bars/daily" and "date" in p:
            return list(self.by_date.get(p["date"], []))
        return list(self.by_path.get(path, []))


def weekdays_back(end: str, n: int):
    """end(取引日) から遡って n 個の平日を新しい順に返す。"""
    d = dt.date.fromisoformat(end)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= dt.timedelta(days=1)
    return out


def bar(code, date, ul=False, vo=1000.0, c=100.0, h=None):
    return {"Code": code, "Date": date, "UL": "1" if ul else "0",
            "C": c, "AdjC": c, "AdjVo": vo, "AdjH": h if h is not None else c}


# ----------------------------------------------------------------------
# V2-1. recent_stop_high()
# ----------------------------------------------------------------------
def test_recent_stop_high_window_boundary():
    """当日を1日目とする20営業日窓。20日目のS高は拾い、21日目は拾わない。"""
    target = "2026-09-11"          # 金曜
    days = weekdays_back(target, 25)   # days[0]=当日, days[19]=20日目, days[20]=21日目
    by_date = {}
    for i, d in enumerate(days):
        rows = [bar("A0", d), bar("B0", d), bar("C0", d), bar("D0", d)]
        if i == 19:
            rows[0] = bar("A0", d, ul=True, vo=90000.0, c=300.0)
        if i == 20:
            rows[1] = bar("B0", d, ul=True, vo=90000.0, c=300.0)
        by_date[d] = rows
    jq = FakeJQ(by_date)

    got = sc.recent_stop_high(jq, {"A0", "B0", "C0", "D0"}, target, window=20)
    assert "A0" in got, "20営業日目のS高は母集団に入る"
    assert got["A0"]["sh_date"] == days[19]
    assert got["A0"]["sh_vol"] == 90000.0 and got["A0"]["sh_close"] == 300.0
    assert "B0" not in got, "21営業日目のS高は窓の外"
    assert "D0" not in got, "S高なしは入らない"
    # 走査は20営業日ぶん（土日は問い合わせない）
    dates_asked = [p["date"] for path, p in jq.calls if path == "/equities/bars/daily"]
    assert len(dates_asked) == 20, dates_asked
    assert all(dt.date.fromisoformat(d).weekday() < 5 for d in dates_asked)


def test_recent_stop_high_takes_latest_episode():
    """複数回S高していれば最新の日を採る。"""
    target = "2026-09-11"
    days = weekdays_back(target, 22)
    by_date = {}
    for i, d in enumerate(days):
        r = bar("C0", d)
        if i in (3, 10):
            r = bar("C0", d, ul=True, vo=5000.0 + i, c=200.0 + i)
        by_date[d] = [r]
    got = sc.recent_stop_high(FakeJQ(by_date), {"C0"}, target, window=20)
    assert got["C0"]["sh_date"] == days[3], "より新しいS高日が残る"


def test_recent_stop_high_skips_holidays_and_respects_universe():
    """祝日(空レスポンス)は営業日として数えない＝連休があっても窓が縮まない。"""
    target = "2026-09-11"
    days = weekdays_back(target, 24)
    by_date = {}
    for i, d in enumerate(days):
        if i in (2, 3):
            by_date[d] = []            # 祝日扱い（データが返らない）
            continue
        rows = [bar("A0", d, ul=(i == 5)), bar("Z0", d, ul=True)]
        if i == 21:                    # 祝日2日ぶん後ろにずれた「20営業日目」
            rows.append(bar("E0", d, ul=True, vo=7777.0, c=321.0))
        else:
            rows.append(bar("E0", d))
        by_date[d] = rows
    jq = FakeJQ(by_date)
    got = sc.recent_stop_high(jq, {"A0", "E0"}, target, window=20)
    assert "A0" in got and got["A0"]["sh_date"] == days[5]
    assert "E0" in got and got["E0"]["sh_date"] == days[21], \
        "祝日を営業日に数えてしまうと窓が18営業日に縮んで取りこぼす"
    assert "Z0" not in got, "ユニバース外は母集団に入れない"
    asked = [p["date"] for path, p in jq.calls if path == "/equities/bars/daily"]
    assert len(asked) == 22, f"祝日2日ぶん余計に問い合わせる: {len(asked)}"


def test_recent_stop_high_without_universe_does_not_filter():
    """uni_codes が空だと市場で絞らない（呼び出し側が0件で止める責任を持つ）。"""
    target = "2026-09-11"
    days = weekdays_back(target, 5)
    by_date = {d: [bar("A0", d, ul=(i == 1)), bar("Z0", d, ul=(i == 1))]
               for i, d in enumerate(days)}
    got = sc.recent_stop_high(FakeJQ(by_date), set(), target, window=3)
    assert set(got) == {"A0", "Z0"}


def test_recent_stop_high_empty_when_no_data():
    assert sc.recent_stop_high(FakeJQ({}), {"A0"}, "2026-09-11", window=5) == {}


# ----------------------------------------------------------------------
# V2-2 / V2-3. dip_metrics()
# ----------------------------------------------------------------------
def _series(n=10):
    dates = weekdays_back("2026-09-11", n)[::-1]      # 昇順
    return dates


def test_dip_metrics_basic():
    dates = _series(6)
    #        d0     d1(S高)  d2     d3     d4     d5(当日)
    highs = [100.0, 130.0, 128.0, 120.0, 118.0, 116.0]
    closes = [98.0, 130.0, 120.0, 115.0, 112.0, 110.0]
    vols = [1000.0, 50000.0, 30000.0, 20000.0, 12000.0, 10000.0]
    m = sc.dip_metrics(dates, highs, closes, vols, dates[1])
    assert m["sh_idx"] == 1
    assert m["peak"] == 130.0 and m["peak_date"] == dates[1]
    assert m["days_from_peak"] == 4                       # 添字差 5-1
    assert abs(m["dd_pct"] - (110.0 / 130.0 - 1) * 100) < 1e-9
    assert abs(m["dry_pct"] - 20.0) < 1e-9                # 10000/50000
    avg5 = (30000 + 20000 + 12000 + 10000 + 50000) / 5    # 直近5本（S高日を含む）
    assert abs(m["dry5_pct"] - avg5 / 50000 * 100) < 1e-9


def test_dip_metrics_on_stop_high_day_itself():
    """S高当日: 日数0・下落率0(終値=高値)・枯れ比100%。0%と—を取り違えないこと。"""
    dates = _series(3)
    highs = [100.0, 105.0, 130.0]
    closes = [99.0, 104.0, 130.0]
    vols = [1000.0, 1200.0, 50000.0]
    m = sc.dip_metrics(dates, highs, closes, vols, dates[-1])
    assert m["days_from_peak"] == 0
    assert m["dd_pct"] == 0.0
    assert m["dry_pct"] == 100.0
    assert m["dry5_pct"] is not None


def test_dip_metrics_peak_after_stop_high_is_used():
    """S高の翌日にさらに高値をつけたらそちらが起点になる。"""
    dates = _series(5)
    highs = [100.0, 130.0, 145.0, 140.0, 138.0]
    closes = [99.0, 130.0, 142.0, 130.0, 120.0]
    vols = [1000.0, 50000.0, 40000.0, 20000.0, 9000.0]
    m = sc.dip_metrics(dates, highs, closes, vols, dates[1])
    assert m["peak"] == 145.0 and m["peak_date"] == dates[2]
    assert m["days_from_peak"] == 2
    assert abs(m["dd_pct"] - (120.0 / 145.0 - 1) * 100) < 1e-9


def test_dip_metrics_repeated_peak_uses_latest_day():
    dates = _series(4)
    highs = [100.0, 130.0, 120.0, 130.0]
    closes = [99.0, 129.0, 118.0, 125.0]
    vols = [1000.0, 50000.0, 20000.0, 30000.0]
    m = sc.dip_metrics(dates, highs, closes, vols, dates[1])
    assert m["peak_date"] == dates[3] and m["days_from_peak"] == 0


def test_dip_metrics_zero_volume_denominator_returns_none():
    """V2-3: S高日の出来高が0でも例外を出さず None を返す（0% と表示しない）。"""
    dates = _series(4)
    highs = [100.0, 130.0, 125.0, 120.0]
    closes = [99.0, 130.0, 118.0, 110.0]
    vols = [1000.0, 0.0, 20000.0, 9000.0]
    m = sc.dip_metrics(dates, highs, closes, vols, dates[1])
    assert m["dry_pct"] is None and m["dry5_pct"] is None
    assert m["dd_pct"] is not None, "枯れ比が出せなくても下落率は出る"


def test_dip_metrics_unknown_or_empty_inputs():
    dates = _series(3)
    z = sc.dip_metrics(dates, [1.0] * 3, [1.0] * 3, [1.0] * 3, "1999-01-01")
    assert z["sh_idx"] is None and z["dd_pct"] is None and z["dry_pct"] is None
    assert sc.dip_metrics([], [], [], [], "2026-09-11")["sh_idx"] is None


def test_dip_metrics_all_highs_none():
    dates = _series(3)
    m = sc.dip_metrics(dates, [None] * 3, [10.0] * 3, [5.0] * 3, dates[0])
    assert m["peak"] is None and m["dd_pct"] is None and m["days_from_peak"] is None
    assert m["dry_pct"] == 100.0, "高値が無くても枯れ比は出せる"


# ----------------------------------------------------------------------
# turnover_oku20() / bdays_between()
# ----------------------------------------------------------------------
def test_turnover_oku20():
    closes = [1000.0] * 20
    vols = [100000.0] * 20
    assert abs(sc.turnover_oku20(closes, vols) - 1.0) < 1e-9   # 1e8円 = 1億
    assert sc.turnover_oku20(closes[:19], vols[:19]) is None   # 20本未満は None
    v = list(vols); v[-1] = None
    assert sc.turnover_oku20(closes, v) is None                # 欠損があれば None


def test_turnover_uses_latest_20_bars_inclusive():
    closes = [1000.0] * 25
    vols = [0.0] * 5 + [100000.0] * 20        # 直近20本だけが効く
    assert abs(sc.turnover_oku20(closes, vols) - 1.0) < 1e-9


def test_bdays_between():
    fri, mon = dt.date(2026, 9, 11), dt.date(2026, 9, 14)
    assert sc.bdays_between(fri, fri) == 0
    assert sc.bdays_between(fri, mon) == 1              # 土日を跨ぐ
    assert sc.bdays_between(fri, dt.date(2026, 9, 18)) == 5
    assert sc.bdays_between(mon, fri) is None           # 過去日
    assert sc.bdays_between(None, fri) is None


# ----------------------------------------------------------------------
# V2-5. days_to_earnings() / pick_next_earnings()
# ----------------------------------------------------------------------
def test_days_to_earnings():
    today = dt.date(2026, 9, 11)
    assert sc.days_to_earnings(None, today) is None            # 予定なし
    assert sc.days_to_earnings("", today) is None
    assert sc.days_to_earnings("8月上旬", today) is None        # 日付として読めない
    assert sc.days_to_earnings("2026-09-01", today) is None    # 過去日のみ
    assert sc.days_to_earnings("2026-09-11", today) == 0       # 当日
    assert sc.days_to_earnings("2026-09-18", today) == 5
    assert sc.days_to_earnings(dt.date(2026, 9, 14), today) == 1


def test_pick_next_earnings_multiple_rows():
    """複数件あれば最も近い未来を採る。過去だけの銘柄は載せない。"""
    today = dt.date(2026, 9, 11)
    rows = [{"Code": "13010", "Date": "2026-08-01"},
            {"Code": "13010", "Date": "2026-11-10"},
            {"Code": "13010", "Date": "2026-10-15"},
            {"Code": "99970", "Date": "2026-08-17"},
            {"Code": "", "Date": "2026-10-01"},
            {"Code": "46510", "Date": None}]
    cal = sc.pick_next_earnings(rows, today)
    assert cal == {"13010": "2026-10-15"}


def test_earnings_map_handles_api_failure():
    class Boom:
        def get(self, *a, **k):
            raise RuntimeError("403")
    assert sc.earnings_map(Boom(), dt.date(2026, 9, 11)) == {}


# ----------------------------------------------------------------------
# V2-6. 手入力YAML
# ----------------------------------------------------------------------
def test_load_manual_missing_empty_and_broken():
    d = tempfile.mkdtemp()
    try:
        assert sc.load_manual("9999", d) == {}, "ファイル無し"
        open(os.path.join(d, "9998.yaml"), "w").close()
        assert sc.load_manual("9998", d) == {}, "空ファイル"
        with open(os.path.join(d, "9997.yaml"), "w", encoding="utf-8") as f:
            f.write("material: [unclosed\n")
        assert sc.load_manual("9997", d) == {}, "壊れたYAML"
        with open(os.path.join(d, "9996.yaml"), "w", encoding="utf-8") as f:
            f.write("- a\n- b\n")
        assert sc.load_manual("9996", d) == {}, "最上位がリスト"
    finally:
        shutil.rmtree(d)


def test_load_manual_keys_and_code_forms():
    d = tempfile.mkdtemp()
    try:
        with open(os.path.join(d, "338A.yaml"), "w", encoding="utf-8") as f:
            f.write('code: "338A"\n'
                    'material: |\n  上期経常を上方修正。\n'
                    'material_class: "上方修正"\n'
                    'earnings_date: "2026-10-15"\n'
                    'earnings_date_confirmed: true\n'
                    'float_shares: 12000000\n')
        m = sc.load_manual("338A0", d)          # 5桁でも4桁ファイルを読む
        assert m["material_class"] == "上方修正"
        assert m["earnings_date"] == dt.date(2026, 10, 15)
        assert m["earnings_date_confirmed"] is True
        assert m["float_shares"] == 12000000
        assert sc.load_manual("338A", d) == m
        # material_class 欠損でも落ちない
        with open(os.path.join(d, "1234.yaml"), "w", encoding="utf-8") as f:
            f.write('earnings_date: "8月上旬"\n')
        m2 = sc.load_manual("12340", d)
        assert m2["earnings_date"] is None, "読めない日付は None（キーは残る）"
        assert "material_class" not in m2
    finally:
        shutil.rmtree(d)


# ----------------------------------------------------------------------
# build_shortlist() / market_universe()
# ----------------------------------------------------------------------
def test_build_shortlist_drops_codes_without_today_bar():
    target, prev = "2026-09-11", "2026-09-10"
    by_date = {
        target: [{"Code": "A0", "C": 110.0, "UL": "0", "Vo": 100.0},
                 {"Code": "C0", "C": 90.0, "UL": "1", "Vo": 900.0}],
        prev: [{"Code": "A0", "C": 100.0}, {"Code": "C0", "C": 80.0}],
    }
    sh_map = {"A0": {"sh_date": "2026-09-01", "sh_vol": 1.0, "sh_close": 1.0},
              "B0": {"sh_date": "2026-09-02", "sh_vol": 1.0, "sh_close": 1.0},
              "C0": {"sh_date": target, "sh_vol": 9.0, "sh_close": 90.0}}
    sl, chg = sc.build_shortlist(FakeJQ(by_date), target, prev, {"A0", "B0", "C0"},
                                 sh_map, dict(sc.DEFAULT_CRITERIA))
    codes = {s["code"] for s in sl}
    assert codes == {"A0", "C0"}, "当日バーが無い B0 は落ちる"
    assert abs(chg["A0"] - 10.0) < 1e-9
    a = [s for s in sl if s["code"] == "A0"][0]
    assert a["change_pct"] == 10.0 and a["stop_high"] is False
    c = [s for s in sl if s["code"] == "C0"][0]
    assert c["stop_high"] is True and c["sh_date"] == target


def test_change_pct_is_adjustment_corrected():
    """権利落ち・分割を跨いだ日の前日比を生値同士で出さない。

    実測(2026-09-11 / 14470): 生値 C 同士だと -3.51% だが、正しい前日比は +44.65%。
    当日行の AdjFactor で前日終値を割り戻すと +44.63% になる。
    """
    target, prev = "2026-09-11", "2026-09-10"
    by_date = {
        target: [{"Code": "14470", "C": 357.0, "UL": "0", "Vo": 1.0,
                  "AdjFactor": 0.6671171171171171},
                 {"Code": "A0", "C": 110.0, "UL": "0", "Vo": 1.0, "AdjFactor": 1.0},
                 {"Code": "B0", "C": 110.0, "UL": "0", "Vo": 1.0}],          # AdjFactor 欠損
        prev: [{"Code": "14470", "C": 370.0}, {"Code": "A0", "C": 100.0},
               {"Code": "B0", "C": 100.0}],
    }
    sh_map = {c: {"sh_date": prev, "sh_vol": 1.0, "sh_close": 1.0}
              for c in ("14470", "A0", "B0")}
    _, chg = sc.build_shortlist(FakeJQ(by_date), target, prev, set(sh_map),
                                sh_map, dict(sc.DEFAULT_CRITERIA))
    assert abs(chg["14470"] - 44.63) < 0.02, f"生値同士なら -3.51% になる: {chg['14470']}"
    assert abs(chg["A0"] - 10.0) < 1e-9, "調整なしの日は従来どおり"
    assert abs(chg["B0"] - 10.0) < 1e-9, "AdjFactor 欠損は 1.0 とみなす"


def test_change_pct_excluded_when_factor_unusable():
    target, prev = "2026-09-11", "2026-09-10"
    by_date = {target: [{"Code": "A0", "C": 110.0, "UL": "0", "AdjFactor": 0.0}],
               prev: [{"Code": "A0", "C": 100.0}]}
    sh_map = {"A0": {"sh_date": prev, "sh_vol": 1.0, "sh_close": 1.0}}
    _, chg = sc.build_shortlist(FakeJQ(by_date), target, prev, {"A0"}, sh_map,
                                dict(sc.DEFAULT_CRITERIA))
    assert "A0" not in chg, "調整係数が0なら数字を作らずセクター母数から外す"


def test_market_universe_returns_sector_for_all_codes():
    jq = FakeJQ(by_path={"/equities/master": [
        {"Code": "A0", "CoName": "あ", "MktNm": "グロース", "S33Nm": "情報･通信業"},
        {"Code": "B0", "CoName": "い", "MktNm": "プライム", "S33Nm": "情報・通信業"},
    ]})
    codes, names, mkt, sec = sc.market_universe(jq, "2026-09-11", ["グロース"])
    assert codes == {"A0"} and mkt == {"A0": "グロース"}
    assert names["B0"] == "い", "社名は全市場ぶん持つ"
    assert sec["A0"] == sec["B0"] == "情報・通信業", "半角中黒を全角に正規化"


def test_normalize_sector():
    assert sc.normalize_sector("情報･通信業") == "情報・通信業"
    assert sc.normalize_sector(None) == "" and sc.normalize_sector("") == ""


# ----------------------------------------------------------------------
def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
