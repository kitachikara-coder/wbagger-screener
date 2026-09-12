#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""層A（一覧のレンダリング）のエンドツーエンド検証。ネット不要（API はモック）。

`python3 -m pytest tests/ -q` でも `python3 tests/test_screener_render.py` でも実行可。
"""

import os
import re
import sys
import json
import shutil
import datetime as dt
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import screener as sc   # noqa: E402

TARGET = "2026-09-11"


def weekdays(end, n):
    d, out = dt.date.fromisoformat(end), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= dt.timedelta(days=1)
    return out[::-1]          # 昇順


def synth_bars(code, n=260, sh_offset=None):
    """決定的な日足。sh_offset を渡すとその位置(末尾からの本数)を S高日にする。"""
    dates = weekdays(TARGET, n)
    rows = []
    for i, d in enumerate(dates):
        c = 1000.0 + (i % 17) * 5.0 + i * 0.5
        vo = 50000.0 + (i % 11) * 1000.0
        ul = False
        if sh_offset is not None and i == n - 1 - sh_offset:
            c, vo, ul = c * 1.25, vo * 12, True
        rows.append({"Code": code, "Date": d, "UL": "1" if ul else "0",
                     "O": c * 0.99, "H": c * 1.01, "L": c * 0.98, "C": c, "Vo": vo,
                     "AdjO": c * 0.99, "AdjH": c * 1.01, "AdjL": c * 0.98,
                     "AdjC": c, "AdjVo": vo})
    return rows


FINS = [{"CurPerType": "FY", "DiscDate": "2025-05-10", "Sales": 10000.0, "OP": 1500.0,
         "NP": 1000.0, "Eq": 8000.0, "EqAR": 0.55, "CFO": 1200.0, "ShOutFY": 20000000.0,
         "TrShFY": 0.0},
        {"CurPerType": "FY", "DiscDate": "2026-05-10", "Sales": 12000.0, "OP": 1900.0,
         "NP": 1300.0, "Eq": 9000.0, "EqAR": 0.58, "CFO": 1500.0, "ShOutFY": 20000000.0,
         "TrShFY": 0.0}]


class FakeJQ:
    def __init__(self, bars_by_code, fins=None, margin=None):
        self.bars_by_code = bars_by_code
        self.fins = FINS if fins is None else fins
        self.margin = margin or []
        self.calls = []

    def get(self, path, params=None):
        p = dict(params or {})
        self.calls.append((path, p))
        if path == "/equities/bars/daily":
            if "code" in p:
                return list(self.bars_by_code.get(p["code"], []))
            return [b for rows in self.bars_by_code.values()
                    for b in rows if b["Date"] == p.get("date")]
        if path == "/fins/summary":
            return list(self.fins)
        if path == "/markets/margin-interest":
            return list(self.margin)
        return []


def build_records(manual_dir=None, sectors=None, ecal=None, chg_all=None):
    # コードは実データと同じ5桁（表示は disp_code で4桁になる）。
    # 13010→"1301" / 338A0→"338A" / 99970→"9997"
    bars = {"13010": synth_bars("13010", sh_offset=8),     # 押し目進行中
            "338A0": synth_bars("338A0", sh_offset=0),     # 当日S高・新形式コード
            "99970": synth_bars("99970", n=12, sh_offset=3)}   # 履歴が20本に満たない銘柄
    jq = FakeJQ(bars)
    names = {"13010": "アルファ", "338A0": "ベータ", "99970": "ガンマ"}
    mkt = {"13010": "グロース", "338A0": "スタンダード", "99970": "グロース"}
    sec = sectors or {"13010": "情報・通信業", "338A0": "電気機器", "99970": "情報・通信業"}
    recs = []
    for code in ("13010", "338A0", "99970"):
        rows = bars[code]
        sh_date = [r["Date"] for r in rows if r["UL"] == "1"][-1]
        item = {"code": code, "close": rows[-1]["C"], "change_pct": 1.5,
                "stop_high": rows[-1]["UL"] == "1", "volume": rows[-1]["Vo"],
                "sh_date": sh_date, "sh_vol": None, "sh_close": None,
                "date_target": TARGET}
        recs.append(sc.analyze_candidate(jq, item, names, mkt, dict(sc.DEFAULT_CRITERIA),
                                         sec=sec, ecal=ecal or {}, chg_all=chg_all or {},
                                         manual_dir=manual_dir))
    return recs, jq


def render_to_tmp(recs, crit=None):
    d = tempfile.mkdtemp()
    sc.render(TARGET, recs, crit or dict(sc.DEFAULT_CRITERIA), docs=d)
    with open(os.path.join(d, "index.html"), encoding="utf-8") as f:
        html = f.read()
    with open(os.path.join(d, "data", "latest.json"), encoding="utf-8") as f:
        latest = json.load(f)
    shutil.rmtree(d)
    return html, latest


def parse_rows(html):
    i = html.index("const ROWS = ") + len("const ROWS = ")
    j = html.index(";\nconst TARGET", i)
    return json.loads(html[i:j])


# ----------------------------------------------------------------------
# G3: 自動判定の撤去
# ----------------------------------------------------------------------
def test_auto_judgement_is_gone():
    assert not hasattr(sc, "label"), "label() は撤去する（G3）"
    recs, _ = build_records()
    html, latest = render_to_tmp(recs)
    for banned in ("初動入口", "押し目待ち", "監視△", "<th>判定</th>", '"label"'):
        assert banned not in html, banned
    for r in latest["candidates"]:
        assert "label" not in r


# ----------------------------------------------------------------------
# 列の入れ替え（層A に残す / 層Bへ移す）
# ----------------------------------------------------------------------
def test_layer_a_columns():
    recs, _ = build_records()
    html, _ = render_to_tmp(recs)
    i = html.index("const COLS = ")
    cols = html[i:html.index("];", i)]
    for must in ("ストップ高日", "高値から(日)", "高値から(%)", "枯れ比 当日",
                 "枯れ比 5日平均", "代金20日(億)", "決算まで(営業日)", "材料分類", "タブー"):
        assert must in cols, must
    for moved in ("P.O.", "MACD", "出来高倍", "自己資本", "増益", "逆指値目安"):
        assert moved not in cols, f"{moved} は層Bへ移す（一覧には出さない）"


def test_rows_payload_shape_and_values():
    recs, _ = build_records()
    html, _ = render_to_tmp(recs)
    rows = parse_rows(html)
    assert len(rows) == 3
    by = {r["c"]: r for r in rows}
    a = by["1301"]
    assert a["sh"] and a["dp"] is not None and a["dd"] is not None
    assert a["dry"] is not None and a["dry5"] is not None
    assert a["to"] is not None, "20日平均売買代金"
    assert a["mcl"] == sc.MATERIAL_UNSET, "手入力が無ければ「未」"
    b = by["338A"]
    assert b["sh"] == TARGET and b["dp"] == 0, "当日S高は 高値からの日数 0"
    assert b["dry"] == 100.0, "当日S高は枯れ比100%"
    assert b["dry5"] != b["dry"], "5日平均は当日値の複製ではない"
    assert abs(b["dry5"] - 26.37) < 0.01, "5日平均の配線（dry5_pct が dry5 列に乗る）"
    c = by["9997"]
    assert c["to"] is None, "20本未満の履歴では売買代金は算出不能"
    # 表示コード(4桁) と 生コード(5桁) の取り違え検知
    assert {r["rc"] for r in rows} == {"13010", "338A0", "99970"}
    assert {r["c"] for r in rows} == {"1301", "338A", "9997"}


def test_row_codes_match_chart_keys():
    """行クリックの引数(rc)が DATA のキー集合と一致すること（食い違うと全行チャートが開かない）。"""
    recs, _ = build_records()
    html, _ = render_to_tmp(recs)
    rows = parse_rows(html)
    data = json.loads(html[html.index("const DATA = ") + len("const DATA = "):
                           html.index(";\nconst ROWS")])
    assert {r["rc"] for r in rows} == set(data)
    assert "openRow(\\'' + r.rc + '\\')" in html
    assert "function openRow(code) { showChart(code); renderCard(code); }" in html


def test_default_sort_is_shdate_desc_then_dry_asc():
    recs, _ = build_records()
    html, latest = render_to_tmp(recs)
    order = [r["code"] for r in latest["candidates"]]
    assert order[0] == "338A", "当日S高が先頭（S高日が新しい順）"
    shs = [r["sh_date"] for r in latest["candidates"]]
    assert shs == sorted(shs, reverse=True)
    # 画面の初期表示順は Python 側の並びそのまま（ヘッダを押すまで JS は並べ替えない）
    assert [r["c"] for r in parse_rows(html)] == order
    assert "userSorted ? ROWS.filter(passes).sort(cmp) : ROWS.filter(passes)" in html
    # 同じS高日なら枯れ比が低い方が先
    same = [dict(recs[0]), dict(recs[0])]
    same[0]["code"], same[0]["raw_code"], same[0]["dry_pct"] = "X1", "X10", 80.0
    same[1]["code"], same[1]["raw_code"], same[1]["dry_pct"] = "X2", "X20", 15.0
    _, lt = render_to_tmp(same)
    assert [r["code"] for r in lt["candidates"]] == ["X2", "X1"]


def test_sort_puts_missing_values_last():
    recs, _ = build_records()
    r = dict(recs[0])
    r["code"], r["raw_code"], r["dry_pct"] = "Z9", "Z90", None
    r["sh_date"] = recs[0]["sh_date"]
    mix = [r, recs[0]]
    _, lt = render_to_tmp(mix)
    assert lt["candidates"][-1]["code"] == "Z9", "値が無い行は最後"


# ----------------------------------------------------------------------
# V2-8. 表示の桁（None を 0% と誤表示しない）
# ----------------------------------------------------------------------
def test_none_renders_as_em_dash_not_zero():
    recs, _ = build_records()
    r = dict(recs[0])
    r["dd_pct"] = r["dry_pct"] = r["dry5_pct"] = r["turnover_oku"] = None
    r["earn_bdays"] = r["days_from_peak"] = None
    html, _ = render_to_tmp([r])
    rows = parse_rows(html)
    assert rows[0]["dd"] is None and rows[0]["dry"] is None and rows[0]["to"] is None
    # 描画側: null は NA、0 は 0 として出す（cellHtml の分岐）
    assert "const NA = \"—\";" in html
    assert "if (na) { txt = (col.k === 'mcl') ? MATERIAL_UNSET : NA;" in html
    assert "「—」はデータ取得不可・算出不能" in html


def test_taboo_has_three_states():
    """タブーは「財務が取れていない=—」「該当なし=空欄」「該当=理由」の3値。"""
    recs, _ = build_records()
    a, b, c = [dict(r) for r in recs]
    a["taboo_hit"], a["taboo_reason"] = None, ""
    b["taboo_hit"], b["taboo_reason"] = False, ""
    c["taboo_hit"], c["taboo_reason"] = True, "自己資本比率20.0%≤30.0%"
    html, _ = render_to_tmp([a, b, c])
    rows = parse_rows(html)
    by = {r["c"]: r for r in rows}
    assert by[a["code"]]["tbh"] is None
    assert by[b["code"]]["tbh"] is False
    assert by[c["code"]]["tbh"] is True and "自己資本比率" in by[c["code"]]["tb"]
    assert "if (r.tbh === null || r.tbh === undefined) return '<td class=\"na\">' + NA" in html
    assert "if (!r.tbh) return '<td></td>';" in html


def test_zero_is_not_treated_as_missing():
    recs, _ = build_records()
    b = [r for r in recs if r["code"] == "338A"][0]
    assert b["dd_pct"] == 0.0 or abs(b["dd_pct"]) < 2.0
    assert b["days_from_peak"] == 0
    html, _ = render_to_tmp(recs)
    rows = parse_rows(html)
    z = [r for r in rows if r["c"] == "338A"][0]
    assert z["dp"] == 0, "0 は None ではない"


# ----------------------------------------------------------------------
# フィルタは既定で絞らない（B群ゲート）
# ----------------------------------------------------------------------
def test_filters_exist_and_default_to_no_narrowing():
    recs, _ = build_records()
    html, _ = render_to_tmp(recs)
    for fid in ("f-mkt", "f-ddmin", "f-ddmax", "f-dry", "f-to", "f-mcl", "f-earn", "f-q"):
        assert f'id="{fid}"' in html, fid
    # 既定値が空＝絞らない。placeholder はあってよいが value は持たせない
    assert 'id="f-ddmin" step="1" placeholder="下限"' in html
    assert 'id="f-dry" step="5" placeholder="%"' in html
    assert 'id="f-earn"> 決算まで' in html and 'checked' not in html.split('id="f-earn"')[1][:20]
    assert "既定フィルタは <b>OFF</b>" in html
    assert "backtest_dip.py" in html and "B群ゲート" in html


def test_default_filter_actually_narrows_when_gate_enabled():
    """ゲートONのときは注記だけでなく入力欄に値が入る（注記がONで実際は絞らない、をやらない）。"""
    recs, _ = build_records()
    crit = dict(sc.DEFAULT_CRITERIA)
    crit["dip_default_filter"] = True
    crit["dip_dd_band"] = [-20.0, -10.0]
    crit["dip_dry_max"] = 30.0
    html, _ = render_to_tmp(recs, crit)
    assert "既定フィルタは <b>ON</b>" in html
    assert 'id="f-ddmin" step="1" placeholder="下限" value="-20"' in html
    assert 'id="f-ddmax" step="1" placeholder="上限" value="-10"' in html
    assert 'id="f-dry" step="5" placeholder="%" value="30"' in html
    # OFF のときは value を持たない＝絞らない
    off, _ = render_to_tmp(recs, dict(sc.DEFAULT_CRITERIA))
    assert 'id="f-ddmin" step="1" placeholder="下限">' in off
    assert 'id="f-dry" step="5" placeholder="%">' in off


def test_dropped_count_is_shown_and_recorded():
    """解析失敗があった日は件数を画面と latest.json に出す（黙って短い一覧にしない）。"""
    recs, _ = build_records()
    d = tempfile.mkdtemp()
    try:
        sc.render(TARGET, recs, dict(sc.DEFAULT_CRITERIA), docs=d, dropped=7)
        with open(os.path.join(d, "index.html"), encoding="utf-8") as f:
            html = f.read()
        with open(os.path.join(d, "data", "latest.json"), encoding="utf-8") as f:
            latest = json.load(f)
    finally:
        shutil.rmtree(d)
    assert "解析失敗 7件" in html
    assert latest["dropped"] == 7
    html0, latest0 = render_to_tmp(recs)
    assert "解析失敗" not in html0 and latest0["dropped"] == 0


def test_latest_json_has_no_chart_series():
    """latest.json にチャート系列を入れない（1銘柄78KB→毎営業日コミットされて肥大するため）。"""
    recs, _ = build_records()
    html, latest = render_to_tmp(recs)
    for r in latest["candidates"]:
        assert "chart" not in r
        assert r["dd_pct"] is not None or r["dd_pct"] is None    # 他の列は残っている
        assert "sh_date" in r and "dry_pct" in r and "turnover_oku" in r
    assert '"chart"' in html, "チャート系列は index.html 側にだけ埋める"


# ----------------------------------------------------------------------
# V1-2. チャート（凍結）が従来どおり
# ----------------------------------------------------------------------
def test_chart_block_is_intact():
    recs, _ = build_records()
    html, _ = render_to_tmp(recs)
    for must in ("function showChart(code, tf)", "id=\"cPrice\"", "id=\"cMacd\"", "id=\"cRci\"",
                 "onclick=\"setTf('d')\"", "onclick=\"setTf('w')\"",
                 "line('MA5','ma5',rows,", "line('MA25','ma25',rows,", "line('MA75','ma75',rows,",
                 "line('RCI9','rci9',rows,", "line('RCI26','rci26',rows,",
                 "チャートデータ不足（新規上場等）", "cdn.jsdelivr.net/npm/chart.js@4"):
        assert must in html, must
    data = json.loads(html[html.index("const DATA = ") + len("const DATA = "):
                           html.index(";\nconst ROWS")])
    assert set(data) == {"13010", "338A0", "99970"}, "DATA のキーは生コード(raw_code)"
    assert data["13010"]["chart"]["d"] and data["13010"]["chart"]["w"]
    pt = data["13010"]["chart"]["d"][-1]
    assert set(pt) >= {"d", "o", "h", "l", "c", "ma5", "ma25", "ma75",
                       "macd", "sig", "hist", "rci9", "rci26"}


def test_empty_chart_for_short_history():
    """新規上場等（5本未満）で chart が空でも落ちず、画面は「データ不足」に倒れる。"""
    jq = FakeJQ({"12340": synth_bars("12340", n=3, sh_offset=0)})
    rows = jq.bars_by_code["12340"]
    item = {"code": "12340", "close": rows[-1]["C"], "change_pct": 0.0, "stop_high": True,
            "volume": rows[-1]["Vo"], "sh_date": rows[-1]["Date"], "sh_vol": None,
            "sh_close": None, "date_target": TARGET}
    rec = sc.analyze_candidate(jq, item, {"12340": "デルタ"}, {"12340": "グロース"},
                               dict(sc.DEFAULT_CRITERIA))
    assert rec["code"] == "1234" and rec["raw_code"] == "12340"
    assert rec["chart"] == []
    html, _ = render_to_tmp([rec])
    assert "チャートデータ不足（新規上場等）" in html


# ----------------------------------------------------------------------
# 手入力（材料分類・決算日）が層Aに出る
# ----------------------------------------------------------------------
def test_manual_material_class_and_earnings():
    d = tempfile.mkdtemp()
    try:
        with open(os.path.join(d, "1301.yaml"), "w", encoding="utf-8") as f:
            f.write('material_class: "上方修正"\n'
                    'earnings_date: "2026-09-18"\n'
                    'earnings_date_confirmed: true\n')
        with open(os.path.join(d, "338A.yaml"), "w", encoding="utf-8") as f:
            f.write('earnings_date: "2026-09-16"\n')       # confirmed 無し → 推定
        recs, _ = build_records(manual_dir=d)
    finally:
        shutil.rmtree(d)
    by = {r["code"]: r for r in recs}
    assert by["1301"]["material_class"] == "上方修正"
    assert by["1301"]["earn_date"] == "2026-09-18" and by["1301"]["earn_src"] == "確定"
    assert by["1301"]["earn_bdays"] == 5
    assert by["338A"]["earn_src"] == "推定", "手入力で confirmed が無ければ推定"
    assert by["9997"]["material_class"] == sc.MATERIAL_UNSET
    assert by["9997"]["earn_date"] is None and by["9997"]["earn_bdays"] is None


def test_earnings_calendar_is_fallback_only():
    recs, _ = build_records(ecal={"13010": "2026-09-14", "99970": "2026-09-15"})
    by = {r["code"]: r for r in recs}
    assert by["1301"]["earn_src"] == "推定(API)" and by["1301"]["earn_date"] == "2026-09-14"
    assert by["9997"]["earn_bdays"] == 2
    assert by["338A"]["earn_date"] is None


def test_manual_wins_over_earnings_calendar():
    """両方ある場合は手入力YAMLが勝つ（依頼書P2「YAML優先 → なければ earnings-calendar」）。"""
    d = tempfile.mkdtemp()
    try:
        with open(os.path.join(d, "1301.yaml"), "w", encoding="utf-8") as f:
            f.write('earnings_date: "2026-10-15"\nearnings_date_confirmed: true\n')
        recs, _ = build_records(manual_dir=d,
                                ecal={"13010": "2026-09-14", "338A0": "2026-09-14"})
    finally:
        shutil.rmtree(d)
    by = {r["code"]: r for r in recs}
    assert by["1301"]["earn_date"] == "2026-10-15" and by["1301"]["earn_src"] == "確定"
    assert by["338A"]["earn_date"] == "2026-09-14" and by["338A"]["earn_src"] == "推定(API)"


def test_price_comes_from_adjusted_series():
    """画面の株価も調整済み終値。判断ログの決済が AdjC なので食い違わせない。"""
    bars = synth_bars("13010", sh_offset=8)
    for r in bars:                      # 生値だけ 2倍にして、どちらを拾っているか判る形にする
        r["C"] = r["AdjC"] * 2
    jq = FakeJQ({"13010": bars})
    item = {"code": "13010", "close": bars[-1]["C"], "change_pct": 0.0, "stop_high": False,
            "volume": bars[-1]["Vo"],
            "sh_date": [r["Date"] for r in bars if r["UL"] == "1"][-1],
            "sh_vol": None, "sh_close": None, "date_target": TARGET}
    rec = sc.analyze_candidate(jq, item, {"13010": "あ"}, {"13010": "グロース"},
                               dict(sc.DEFAULT_CRITERIA))
    assert abs(rec["price"] - round(bars[-1]["AdjC"], 1)) < 0.05, \
        f"生値 {bars[-1]['C']} ではなく調整済み {bars[-1]['AdjC']} を出す: {rec['price']}"


def test_change_pct_uses_adjusted_series_not_raw_bulk_close():
    """権利落ち日に「動いていないのに-50%」を出さない（調整済み系列から出し直す）。"""
    bars = synth_bars("13010", sh_offset=8)
    jq = FakeJQ({"13010": bars})
    item = {"code": "13010", "close": bars[-1]["C"], "change_pct": -50.0,   # 生値由来の嘘の値
            "stop_high": False, "volume": bars[-1]["Vo"],
            "sh_date": [r["Date"] for r in bars if r["UL"] == "1"][-1],
            "sh_vol": None, "sh_close": None, "date_target": TARGET}
    rec = sc.analyze_candidate(jq, item, {"13010": "あ"}, {"13010": "グロース"},
                               dict(sc.DEFAULT_CRITERIA))
    expect = round((bars[-1]["AdjC"] / bars[-2]["AdjC"] - 1) * 100, 2)
    assert rec["change_pct"] == expect != -50.0


def test_missing_manual_dir_does_not_break():
    recs, _ = build_records(manual_dir=os.path.join(tempfile.gettempdir(), "no_such_dir_xyz"))
    assert all(r["material_class"] == sc.MATERIAL_UNSET for r in recs)


# ----------------------------------------------------------------------
# 注記の約束
# ----------------------------------------------------------------------
def test_note_contains_required_disclosures():
    recs, _ = build_records()
    html, _ = render_to_tmp(recs)
    for must in ("投資助言ではない", "最終判断は自己責任",
                 "翌営業日発表分しか返さない限定フィード",
                 "空欄は「発表予定なし」を意味しない",
                 "祝日未対応", "【推測】"):
        assert must in html, must
    assert "合成スコア・総合判定は置かない" in html


def test_empty_records_render():
    html, latest = render_to_tmp([])
    assert latest["candidates"] == []
    assert "該当なし" in html and "const ROWS = []" in html


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
