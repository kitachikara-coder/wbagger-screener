#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""層B（銘柄カード）の単体＋E2E。ネット不要（API はモック）。

`python3 -m pytest tests/ -q` でも `python3 tests/test_screener_card.py` でも実行可。
依頼書 V2 の 4（fetch_margin）を含む。
"""

import os
import sys
import json
import shutil
import datetime as dt
import tempfile

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import screener as sc                                   # noqa: E402
from test_screener_render import (build_records, render_to_tmp, synth_bars,   # noqa: E402
                                  FakeJQ, TARGET)

MARGIN_ROWS = [
    {"Date": "2026-08-21", "Code": "13010", "LongVol": 1000000.0, "ShrtVol": 200000.0,
     "LongStdVol": 600000.0, "ShrtStdVol": 150000.0, "LongNegVol": 400000.0,
     "ShrtNegVol": 50000.0, "IssType": "2"},
    {"Date": "2026-08-28", "Code": "13010", "LongVol": 1400000.0, "ShrtVol": 200000.0,
     "LongStdVol": 800000.0, "ShrtStdVol": 150000.0, "LongNegVol": 600000.0,
     "ShrtNegVol": 50000.0, "IssType": "2"},
    {"Date": "2026-09-04", "Code": "13010", "LongVol": 1800000.0, "ShrtVol": 150000.0,
     "LongStdVol": 900000.0, "ShrtStdVol": 100000.0, "LongNegVol": 900000.0,
     "ShrtNegVol": 50000.0, "IssType": "2"},
]


class MarginJQ:
    def __init__(self, rows=None, exc=None):
        self.rows = MARGIN_ROWS if rows is None else rows
        self.exc = exc
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if self.exc:
            raise self.exc
        return list(self.rows)


def http_error(status):
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(f"{status}", response=resp)


# ----------------------------------------------------------------------
# V2-4. fetch_margin / parse_margin
# ----------------------------------------------------------------------
def test_parse_margin_normal():
    ws = sc.parse_margin(MARGIN_ROWS, weeks=3)
    assert [w["date"] for w in ws] == ["2026-08-21", "2026-08-28", "2026-09-04"]
    assert ws[-1]["long"] == 1800000.0 and ws[-1]["short"] == 150000.0
    assert abs(ws[-1]["ratio"] - 12.0) < 1e-9          # 1,800,000 / 150,000
    assert abs(ws[1]["ratio"] - 7.0) < 1e-9
    assert ws[-1]["d_long"] == 400000.0
    assert abs(ws[-1]["d_ratio"] - 5.0) < 1e-9
    assert ws[0]["d_long"] is None, "最初の週に前週比は無い"
    assert abs(ws[-1]["neg_pct"] - 50.0) < 1e-9        # 一般 900,000 / 買残 1,800,000
    assert abs(ws[-1]["std_ratio"] - 9.0) < 1e-9


def test_parse_margin_zero_short_keeps_week_but_ratio_none():
    rows = [dict(MARGIN_ROWS[-1], ShrtVol=0.0, ShrtStdVol=0.0)]
    ws = sc.parse_margin(rows)
    assert len(ws) == 1, "売残0の週も消さない"
    assert ws[0]["ratio"] is None and ws[0]["std_ratio"] is None, "ゼロ除算は None"
    assert ws[0]["long"] == 1800000.0
    assert sc.parse_margin([dict(MARGIN_ROWS[-1], ShrtVol=None)])[0]["ratio"] is None
    assert sc.parse_margin([dict(MARGIN_ROWS[-1], LongVol="")])[0]["ratio"] is None


def test_parse_margin_dedups_same_date():
    rows = MARGIN_ROWS + [dict(MARGIN_ROWS[-1], IssType="1", LongVol=1.0)]
    ws = sc.parse_margin(rows)
    assert len(ws) == 3 and ws[-1]["long"] == 1.0, "同一日は最後の行を採る"


def test_parse_margin_empty():
    assert sc.parse_margin([]) == [] and sc.parse_margin(None) == []


def test_fetch_margin_success_and_window():
    jq = MarginJQ()
    ws, reason = sc.fetch_margin(jq, "13010", TARGET, weeks=3)
    assert reason == "" and len(ws) == 3
    path, params = jq.calls[0]
    assert path == "/markets/margin-interest"
    assert params["code"] == "13010" and params["to"] == TARGET and "from" in params


def test_fetch_margin_plan_gated_403_gives_reason():
    ws, reason = sc.fetch_margin(MarginJQ(exc=http_error(403)), "13010", TARGET)
    assert ws == [] and "プラン外" in reason
    ws, reason = sc.fetch_margin(MarginJQ(exc=http_error(401)), "13010", TARGET)
    assert ws == [] and "プラン外" in reason


def test_fetch_margin_other_errors_are_distinguished():
    ws, reason = sc.fetch_margin(MarginJQ(exc=http_error(500)), "13010", TARGET)
    assert ws == [] and "取得失敗" in reason and "500" in reason
    ws, reason = sc.fetch_margin(MarginJQ(exc=requests.ConnectionError("boom")), "13010", TARGET)
    assert ws == [] and "取得失敗" in reason
    ws, reason = sc.fetch_margin(MarginJQ(rows=[]), "13010", TARGET)
    assert ws == [] and reason == "データ無し", "空データはプラン外と区別する"


# ----------------------------------------------------------------------
# 出来高プロファイル / 下落日 vs 反発日
# ----------------------------------------------------------------------
def test_volume_profile_bases_on_stop_high_day():
    dates = [f"2026-09-{d:02d}" for d in range(1, 7)]
    vols = [100.0, 1000.0, 500.0, 250.0, 100.0, 50.0]
    vp = sc.volume_profile(dates, vols, sh_idx=1, n=4)
    assert [p["pct"] for p in vp] == [50.0, 25.0, 10.0, 5.0]
    assert [p["sh"] for p in vp] == [False] * 4, "S高日が窓の外でも基準はS高日"
    vp2 = sc.volume_profile(dates, vols, sh_idx=1, n=6)
    assert vp2[1]["sh"] is True and vp2[1]["pct"] == 100.0


def test_volume_profile_zero_base_and_missing():
    dates = ["2026-09-01", "2026-09-02"]
    assert sc.volume_profile(dates, [0.0, 5.0], 0)[0]["pct"] is None
    assert sc.volume_profile(dates, [None, 5.0], 0)[0]["pct"] is None
    assert sc.volume_profile(dates, [1.0, 2.0], None) == []
    assert sc.volume_profile([], [], 0) == []


def test_dip_volume_split():
    closes = [100.0, 130.0, 120.0, 110.0, 115.0, 105.0]
    vols = [10.0, 900.0, 300.0, 200.0, 60.0, 100.0]
    r = sc.dip_volume_split(closes, vols, sh_idx=1)
    assert r["down_n"] == 3 and r["up_n"] == 1            # idx2,3,5 が下落 / idx4 が反発
    assert abs(r["down_avg"] - 200.0) < 1e-9
    assert r["up_avg"] == 60.0
    empty = sc.dip_volume_split(closes, vols, sh_idx=len(closes) - 1)
    assert empty["down_n"] == 0 and empty["down_avg"] is None
    assert sc.dip_volume_split(closes, vols, None)["down_avg"] is None


# ----------------------------------------------------------------------
# 位置要約 / セクター連動 / 過去エピソード
# ----------------------------------------------------------------------
def test_position_summary():
    closes = [100.0] * 80 + [90.0]         # 5/25/75MA すべて上にある＝株価は下
    s = sc.position_summary(closes, -14.2)
    assert s == "5MA下・25MA下・75MA下・高値-14.2%"
    s2 = sc.position_summary([100.0] * 3, None)
    assert "5MA" + sc.NA in s2 and "高値" + sc.NA in s2
    assert sc.position_summary([], None) == sc.NA


def test_sector_comove():
    sec = {"A": "情報・通信業", "B": "情報・通信業", "C": "情報・通信業", "D": "電気機器"}
    chg = {"A": 5.0, "B": 1.0, "C": 3.0, "D": 9.0}
    r = sc.sector_comove("A", sec, chg)
    assert r["name"] == "情報・通信業" and r["hot"] == 2 and r["total"] == 3
    assert sc.sector_comove("Z", sec, chg)["hot"] is None, "業種不明は—"
    assert sc.sector_comove("A", sec, {})["hot"] is None, "当日の騰落が無ければ—"


def test_past_sh_episodes():
    n = 120
    dates = [f"d{i:03d}" for i in range(n)]
    highs = [100.0] * n
    closes = [100.0] * n
    uls = [False] * n
    uls[10] = True                 # 完了したエピソード
    uls[11] = True                 # 直後の連続S高は同一エピソードにまとめる
    uls[n - 5] = True              # 進行中（結果が出ていない）
    highs[10] = 150.0
    for i in range(11, 31):
        closes[i] = 120.0
    closes[15] = 90.0              # 最大押し
    closes[10], closes[15] = 140.0, 90.0
    eps = sc.past_sh_episodes(dates, highs, closes, uls)
    assert [e["d"] for e in eps] == ["d010"], "進行中のエピソードは出さない"
    assert abs(eps[0]["dd"] - (90.0 / 150.0 - 1) * 100) < 1e-9
    assert eps[0]["r5"] is not None
    assert sc.past_sh_episodes(dates, highs, closes, [False] * n) == []


# ----------------------------------------------------------------------
# E2E: カードがHTMLに出る
# ----------------------------------------------------------------------
def _records_with_margin(manual_dir=None):
    bars = {"13010": synth_bars("13010", sh_offset=8)}
    jq = FakeJQ(bars, margin=MARGIN_ROWS)
    rows = bars["13010"]
    item = {"code": "13010", "close": rows[-1]["C"], "change_pct": 1.0,
            "stop_high": False, "volume": rows[-1]["Vo"],
            "sh_date": [r["Date"] for r in rows if r["UL"] == "1"][-1],
            "sh_vol": None, "sh_close": None, "date_target": TARGET}
    rec = sc.analyze_candidate(jq, item, {"13010": "アルファ"}, {"13010": "グロース"},
                               dict(sc.DEFAULT_CRITERIA),
                               sec={"13010": "情報・通信業", "99970": "情報・通信業"},
                               ecal={}, chg_all={"13010": 1.0, "99970": 7.0},
                               manual_dir=manual_dir)
    return rec, jq


def test_card_payload_is_populated():
    rec, jq = _records_with_margin()
    c = rec["card"]
    assert c["margin"]["weeks"] and c["margin"]["reason"] == ""
    assert abs(c["margin"]["weeks"][-1]["ratio"] - 12.0) < 1e-3
    assert rec["margin_long_k"] == 1800.0, "G2修正: LongVol が読めている"
    assert c["vol_profile"] and any(p["sh"] for p in c["vol_profile"])
    assert c["vol_split"]["down_n"] > 0
    assert "MA" in c["pos"] and "高値" in c["pos"]
    assert c["sector"]["name"] == "情報・通信業"
    assert c["sector"]["hot"] == 1 and c["sector"]["total"] == 2
    assert c["funda"]["eqar"] is not None and c["funda"]["stop_loss"] is not None
    assert c["margin"]["float_pct"] is None, "浮動株が未入力なら出さない"


def test_card_html_sections_and_notes():
    rec, _ = _records_with_margin()
    html, _ = render_to_tmp([rec])
    for must in ("<div id=\"card\"></div>", "function renderCard(code)",
                 "function openRow(code)", "id=\"cVol\"",
                 "材料（手入力）", "信用残（週次・金曜時点）", "出来高（S高日=100）",
                 "セクター連動・過去の類似局面", "位置とファンダ（一覧から移動）"):
        assert must in html, must
    # 仕様で要求された注記
    assert "日証金の貸借倍率とは別物" in html
    assert "参考のみ・予測に使わない" in html
    assert "「枯れている＝買い」ではない" in html
    # 一覧から層Bへ移した項目がカード側にある
    for k in ("自己資本比率", "営業利益率", "ROE", "増益", "出来高倍(20日平均比)",
              "P.O. / MACD", "逆指値目安(直近5日安値)"):
        assert k in html, k
    data = json.loads(html[html.index("const DATA = ") + len("const DATA = "):
                           html.index(";\nconst ROWS")])
    assert data["13010"]["card"]["margin"]["weeks"]


def test_card_shows_reason_when_margin_unavailable():
    bars = {"13010": synth_bars("13010", sh_offset=8)}

    class Gated(FakeJQ):
        def get(self, path, params=None):
            if path == "/markets/margin-interest":
                raise http_error(403)
            return FakeJQ.get(self, path, params)

    jq = Gated(bars)
    rows = bars["13010"]
    item = {"code": "13010", "close": rows[-1]["C"], "change_pct": 1.0, "stop_high": False,
            "volume": rows[-1]["Vo"],
            "sh_date": [r["Date"] for r in rows if r["UL"] == "1"][-1],
            "sh_vol": None, "sh_close": None, "date_target": TARGET}
    rec = sc.analyze_candidate(jq, item, {"13010": "あ"}, {"13010": "グロース"},
                               dict(sc.DEFAULT_CRITERIA))
    assert rec["card"]["margin"]["weeks"] == []
    assert "プラン外" in rec["card"]["margin"]["reason"]
    assert rec["margin_long_k"] is None
    html, _ = render_to_tmp([rec])
    assert "プラン外" in html


def test_float_shares_gives_ratio_and_warning_threshold():
    d = tempfile.mkdtemp()
    try:
        with open(os.path.join(d, "1301.yaml"), "w", encoding="utf-8") as f:
            f.write("float_shares: 10000000\n")          # 買残1,800,000 / 浮動株1,000万 = 18%
        rec, _ = _records_with_margin(manual_dir=d)
        assert abs(rec["card"]["margin"]["float_pct"] - 18.0) < 1e-6
        with open(os.path.join(d, "1301.yaml"), "w", encoding="utf-8") as f:
            f.write('float_shares: "【要検証】"\n')       # 数値でない値で落ちない
        rec2, _ = _records_with_margin(manual_dir=d)
        assert rec2["card"]["margin"]["float_pct"] is None
    finally:
        shutil.rmtree(d)


def test_card_with_manual_material():
    d = tempfile.mkdtemp()
    try:
        with open(os.path.join(d, "1301.yaml"), "w", encoding="utf-8") as f:
            f.write('material: |\n  上期経常を上方修正。\n'
                    'material_class: "上方修正"\n'
                    'continuity: "受注残に裏付けあり"\n')
        rec, _ = _records_with_margin(manual_dir=d)
    finally:
        shutil.rmtree(d)
    assert rec["card"]["material"].startswith("上期経常")
    assert rec["card"]["continuity"] == "受注残に裏付けあり"
    html, _ = render_to_tmp([rec])
    assert "上期経常を上方修正。" in html and "受注残に裏付けあり" in html


def test_card_survives_empty_manual_and_missing_data():
    recs, _ = build_records()
    for r in recs:
        c = r["card"]
        assert c["material"] == "" and c["material_class"] == sc.MATERIAL_UNSET
        assert c["margin"]["weeks"] == [] and c["margin"]["reason"] == "データ無し"
    html, _ = render_to_tmp(recs)
    assert "未入力（manual/&lt;code&gt;.yaml に material を書く）" in html


def test_card_html_is_escaped():
    """社名・手入力がHTMLに素通りしない（</script> や引用符でページを壊さない）。"""
    rec, _ = _records_with_margin()
    rec["name"] = "</script><img src=x onerror=alert(1)>"
    rec["card"]["material"] = 'a"b\'c<script>'
    html, _ = render_to_tmp([rec])
    assert "function esc(s)" in html
    assert html.count("</script>") == 3, "埋め込みJSONが途中でスクリプトを閉じない（script要素は3つだけ）"
    assert "\\u003c/script\\u003e" in html, "JSON側で < が \\u003c にエスケープされている"
    assert "<img src=x onerror=alert(1)>" not in html


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
