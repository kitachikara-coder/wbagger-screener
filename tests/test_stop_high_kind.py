#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S高種別（張り付き／一時）の判定と、層Aでの表示・フィルタ。ネット不要。

`python3 -m pytest tests/ -q` でも `python3 tests/test_stop_high_kind.py` でも実行可。
依頼書 `Code依頼_S高種別列と材料の運用反映_20260913.md` V2 の 1〜5。
"""

import os
import sys
import json
import shutil
import datetime as dt
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import screener as sc                                              # noqa: E402
from test_screener_render import (build_records, render_to_tmp,    # noqa: E402
                                  parse_rows, synth_bars, FakeJQ, TARGET)
from test_screener_core import weekdays_back                       # noqa: E402


# ----------------------------------------------------------------------
# V2-1 / V2-2. stop_high_kind()
# ----------------------------------------------------------------------
def test_stop_high_kind_basic():
    assert sc.stop_high_kind(1000.0, 1000.0) == sc.SH_STICK, "終値=高値 は張り付き"
    assert sc.stop_high_kind(1000.0, 999.0) == sc.SH_TOUCH, "終値<高値 は一時"


def test_stop_high_kind_missing_is_none_not_touch():
    """欠損は None（画面は「—」）。「一時」に倒さない。"""
    assert sc.stop_high_kind(None, 1000.0) is None
    assert sc.stop_high_kind(1000.0, None) is None
    assert sc.stop_high_kind(None, None) is None
    assert sc.stop_high_kind("", "") is None
    assert sc.stop_high_kind("なし", 1000.0) is None


def test_stop_high_kind_float_tolerance():
    """浮動小数の等値比較を避ける（1e-9 の許容）。"""
    assert sc.stop_high_kind(1000.0, 999.9999999999) == sc.SH_STICK
    assert sc.stop_high_kind(1000.0, 999.0) == sc.SH_TOUCH
    assert sc.stop_high_kind(0.1 + 0.2, 0.3) == sc.SH_STICK, "0.30000000000000004 vs 0.3"
    # 終値が高値を上回ることは通常ないが、上回っても張り付き側に落ちる
    assert sc.stop_high_kind(1000.0, 1000.5) == sc.SH_STICK


def test_stop_high_kind_accepts_strings_from_api():
    assert sc.stop_high_kind("1000", "1000") == sc.SH_STICK
    assert sc.stop_high_kind("1000", "990") == sc.SH_TOUCH


def test_no_threshold_shortcut():
    """「高値の99.5%以上なら張り付き」のようなしきい値を入れていないこと。"""
    assert sc.stop_high_kind(1000.0, 999.0) == sc.SH_TOUCH, "99.9%でも一時"
    assert sc.stop_high_kind(1000.0, 995.0) == sc.SH_TOUCH, "99.5%でも一時"


# ----------------------------------------------------------------------
# V2-3. 2026-09-11 実データ相当の固定値で6銘柄が期待どおりに分類される
# ----------------------------------------------------------------------
# 依頼書の表と同じ顔ぶれ。H/C は 2026-09-11 の生値（S高日=当日）
REAL_2026_09_11 = [
    # code,   name,                  H,       C,      期待
    ("338A0", "ＺｅｎｍｕＴｅｃｈ", 3255.0, 3255.0, sc.SH_STICK),
    ("50310", "モイ",               435.0,  435.0,  sc.SH_STICK),
    ("62030", "豊和工業",           2844.0, 2844.0, sc.SH_STICK),
    ("44400", "ヴィッツ",           3070.0, 2624.0, sc.SH_TOUCH),
    ("90820", "大和自動車交通",     3040.0, 2466.0, sc.SH_TOUCH),
    ("60810", "アライドアーキテクツ", 202.0, 163.0,  sc.SH_TOUCH),
]


def test_real_six_stocks_classification():
    for code, name, h, c, want in REAL_2026_09_11:
        got = sc.stop_high_kind(h, c)
        assert got == want, f"{code} {name}: H={h} C={c} → {got}（期待 {want}）"
    kinds = [sc.stop_high_kind(h, c) for _, _, h, c, _ in REAL_2026_09_11]
    assert kinds.count(sc.SH_STICK) == 3 and kinds.count(sc.SH_TOUCH) == 3


def test_recent_stop_high_records_kind_without_extra_api():
    """recent_stop_high が既に触っているバーから判定する＝追加リクエスト0。"""
    target = "2026-09-11"
    days = weekdays_back(target, 6)
    by_date = {}
    for i, d in enumerate(days):
        rows = []
        for code, _, h, c, _ in REAL_2026_09_11:
            if i == 0:
                rows.append({"Code": code, "Date": d, "UL": "1", "H": h, "C": c,
                             "AdjH": h, "AdjC": c, "AdjVo": 1000.0})
            else:
                rows.append({"Code": code, "Date": d, "UL": "0", "H": 1.0, "C": 1.0,
                             "AdjH": 1.0, "AdjC": 1.0, "AdjVo": 1.0})
        rows.append({"Code": "NOH0", "Date": d, "UL": "1" if i == 0 else "0",
                     "H": None, "C": 500.0, "AdjH": None, "AdjC": 500.0, "AdjVo": 1.0})
        by_date[d] = rows
    jq = FakeJQ(by_date)
    uni = {c for c, _, _, _, _ in REAL_2026_09_11} | {"NOH0"}
    got = sc.recent_stop_high(jq, uni, target, window=5)
    for code, name, _, _, want in REAL_2026_09_11:
        assert got[code]["sh_kind"] == want, f"{code} {name}"
    assert got["NOH0"]["sh_kind"] is None, "高値が欠損なら None"
    # 走査した営業日数ぶんしか問い合わせていない（種別のための追加取得は無い）
    assert len(jq.calls) == 5, jq.calls


def test_kind_uses_raw_not_adjusted():
    """調整後(Adj*)ではなく生値の H/C で判定する（UL が生値ベースのフラグのため）。"""
    target = "2026-09-11"
    days = weekdays_back(target, 3)
    by_date = {}
    for i, d in enumerate(days):
        # 生値では張り付き(C==H)、調整後では C<H になるよう仕込む
        by_date[d] = [{"Code": "A0", "Date": d, "UL": "1" if i == 0 else "0",
                       "H": 1000.0, "C": 1000.0,
                       "AdjH": 1000.0, "AdjC": 500.0, "AdjVo": 1.0}]
    got = sc.recent_stop_high(FakeJQ(by_date), {"A0"}, target, window=2)
    assert got["A0"]["sh_kind"] == sc.SH_STICK, "AdjC を見ていたら「一時」になってしまう"


# ----------------------------------------------------------------------
# 層Aへの反映
# ----------------------------------------------------------------------
def _records_with_kinds():
    recs, _ = build_records()
    kinds = [sc.SH_STICK, sc.SH_TOUCH, None]
    for r, k in zip(recs, kinds):
        r["sh_kind"] = k
    return recs


def test_column_exists_right_of_stop_high_date():
    recs = _records_with_kinds()
    html, _ = render_to_tmp(recs)
    i = html.index("const COLS = ")
    cols = html[i:html.index("];", i)]
    assert "'S高種別'" in cols
    # 「ストップ高日」の直後であること
    order = [ln for ln in cols.splitlines() if "{k:'" in ln]
    keys = [ln.split("k:'")[1].split("'")[0] for ln in order]
    assert keys.index("sk") == keys.index("sh") + 1, keys


def test_payload_carries_kind_and_renders_badge():
    recs = _records_with_kinds()
    html, latest = render_to_tmp(recs)
    rows = parse_rows(html)
    got = sorted([r["sk"] for r in rows], key=lambda v: (v is None, v))
    assert got == sorted([sc.SH_STICK, sc.SH_TOUCH, None], key=lambda v: (v is None, v))
    js = html[html.index("function cellHtml"):html.index("function cmp")]
    assert "v === SH_STICK ? 'k-stick' : 'k-touch'" in js, "種別で class を出し分けている"
    assert "class=\"skind " in js
    for r in latest["candidates"]:
        assert "sh_kind" in r
    # 欠損は「—」（「一時」に倒さない）
    assert [r for r in rows if r["sk"] is None], "None の行がある"


def test_badge_colours_are_not_good_bad():
    """緑=良い/赤=悪い を示唆しない同系色であること。"""
    recs = _records_with_kinds()
    html, _ = render_to_tmp(recs)
    i = html.index(".skind{")
    css = html[i:i + 260]
    assert "#39506b" in css and "#2b3440" in css, css
    for banned in ("#3fb950", "#f85149"):      # 既存の 良い/悪い 配色
        assert banned not in css, f"優劣を示唆する色が入っている: {banned}"


def test_filter_exists_and_defaults_to_all():
    recs = _records_with_kinds()
    html, _ = render_to_tmp(recs)
    assert 'id="f-sk"' in html
    assert "const sk = val('f-sk');\n  if (sk && r.sk !== sk) return false;" in html
    assert "fillSelect('f-sk', sks, 'すべて');" in html
    # 既定値を持たない＝絞らない
    assert '<label>S高種別 <select id="f-sk"></select></label>' in html
    assert "'f-sk'," in html.split("].forEach(function (id) {")[0], "入力イベントに繋いでいる"
    assert "['f-mkt', 'f-sk', 'f-mcl']" in html, "リセットで戻る"


def test_default_sort_unchanged():
    """既定ソートは S高日が新しい順 → 枯れ比が低い順のまま。"""
    recs = _records_with_kinds()
    html, latest = render_to_tmp(recs)
    shs = [r["sh_date"] for r in latest["candidates"]]
    assert shs == sorted(shs, reverse=True)
    assert "let sortKey = 'sh', sortDir = -1, userSorted = false;" in html
    assert [r["c"] for r in parse_rows(html)] == [r["code"] for r in latest["candidates"]]


def test_note_line_added_without_the_forbidden_word():
    recs = _records_with_kinds()
    html, _ = render_to_tmp(recs)
    note = html[html.index('<div class="note">'):html.index("</div>\n\n<script>")]
    # 文そのものを見る（ラベルだけ残して本文が壊れても落ちるように）
    for frag in ("<b>S高種別</b> = S高日の終値が当日高値と同値なら",
                 "「張り付き」", "下回れば", "「一時」", "（生値の H/C で判定）",
                 "上限に<b>触れた</b>フラグで、引けまで買われ続けたことを意味しない",
                 "どちらが有利かは<b>未検証</b>", "並び順・抽出条件には使っていない【推測】"):
        assert frag in note, frag
    i = note.index("<b>S高種別</b>")
    assert "業種" not in note[i:i + 400], "この1行に「業種」の2字を連続で含めない"


# ----------------------------------------------------------------------
# V2-4. フィルタの件数（JS の passes と同じ規則を Python で再現して確かめる）
# ----------------------------------------------------------------------
def _passes_kind(rows, sk):
    """_TABLE_JS の passes() の S高種別部分と同じ規則。"""
    return [r for r in rows if not sk or r["sk"] == sk]


def test_kind_filter_counts():
    recs = _records_with_kinds()
    html, _ = render_to_tmp(recs)
    rows = parse_rows(html)
    assert len(_passes_kind(rows, "")) == 3, "既定（すべて）は全件"
    assert len(_passes_kind(rows, sc.SH_STICK)) == 1
    assert len(_passes_kind(rows, sc.SH_TOUCH)) == 1
    assert len(_passes_kind(rows, "存在しない")) == 0
    assert all(r["sk"] is not None for r in _passes_kind(rows, sc.SH_STICK)), \
        "None の行は種別フィルタで残らない"


def test_kind_select_options_are_fixed_order_and_present_only():
    recs = _records_with_kinds()
    html, _ = render_to_tmp(recs)
    assert '"張り付き", "一時"' in html.split("const SH_KINDS = ")[1][:40], \
        "固定順（張り付き→一時）で埋め込む"
    assert "SH_KINDS.filter(function (k) {" in html
    # 片方しか無ければその1つだけが選択肢になる
    only = [dict(recs[0])]
    only[0]["sh_kind"] = sc.SH_TOUCH
    html2, _ = render_to_tmp(only)
    rows2 = parse_rows(html2)
    assert {r["sk"] for r in rows2} == {sc.SH_TOUCH}


# ----------------------------------------------------------------------
# V2-5. 手入力の決算日表示（確定 / 推定 / —）
# ----------------------------------------------------------------------
def test_manual_earnings_three_states():
    d = tempfile.mkdtemp()
    try:
        with open(os.path.join(d, "1301.yaml"), "w", encoding="utf-8") as f:
            f.write('earnings_date: "2026-10-15"\nearnings_date_confirmed: true\n')
        with open(os.path.join(d, "338A.yaml"), "w", encoding="utf-8") as f:
            f.write('earnings_date: "2026-10-15"\nearnings_date_confirmed: false\n')
        with open(os.path.join(d, "9997.yaml"), "w", encoding="utf-8") as f:
            f.write('earnings_date_confirmed: false\n')     # キー自体が無い
        recs, _ = build_records(manual_dir=d)
    finally:
        shutil.rmtree(d)
    by = {r["code"]: r for r in recs}
    assert by["1301"]["earn_src"] == "確定"
    assert by["338A"]["earn_src"] == "推定"
    assert by["9997"]["earn_date"] is None and by["9997"]["earn_src"] is None, \
        "earnings_date キーが無くても落ちず「—」"


def test_repo_manual_files_load():
    """リポジトリに入っている手入力6件が実際に読めること（A-4）。"""
    expect = {"593A": ("テーマ連想", None), "6522": ("大型契約・提携", "2026-10-15"),
              "3907": ("大型契約・提携", "2026-10-08"), "607A": ("テーマ連想", None),
              "462A": ("大型契約・提携", None), "4170": ("仕手・材料不明", None)}
    for code, (cls, ed) in expect.items():
        m = sc.load_manual(code)
        assert m, f"manual/{code}.yaml が読めない"
        assert m.get("material_class") == cls, code
        got = sc._norm_date(m.get("earnings_date"))
        assert (got.isoformat() if got else None) == ed, code
        assert m.get("material", "").strip(), f"{code} の material が空"
    m = sc.load_manual("6522")
    assert m["earnings_date_confirmed"] is True
    assert "★" in m["material"] and len(m["material"].strip().splitlines()) == 4, \
        "複数行ブロックと ★ が壊れていない"


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
