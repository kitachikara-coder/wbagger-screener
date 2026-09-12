#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3 判断ログ（読み込み・5営業日後の突合・画面表示）。ネット不要。

`python3 -m pytest tests/ -q` でも `python3 tests/test_screener_decisions.py` でも実行可。
依頼書 V2 の 7（decisions.json のファイル無し・空配列・5営業日未経過・コード不一致）。
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
                                  synth_bars, TARGET)

DATES = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07",
         "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"]
CLOSES = [100.0, 102.0, 101.0, 105.0, 108.0, 110.0, 112.0, 111.0, 115.0]


def write(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(obj, str):
            f.write(obj)
        else:
            json.dump(obj, f, ensure_ascii=False)


# ----------------------------------------------------------------------
# V2-7. load_decisions の頑健性
# ----------------------------------------------------------------------
def test_load_decisions_missing_empty_broken():
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "decisions.json")
        assert sc.load_decisions(p) == [], "ファイル無し"
        write(p, {"decisions": []})
        assert sc.load_decisions(p) == [], "空配列"
        write(p, "{ broken")
        assert sc.load_decisions(p) == [], "壊れたJSON"
        write(p, {"decisions": "not a list"})
        assert sc.load_decisions(p) == [], "decisions が配列でない"
        write(p, [])
        assert sc.load_decisions(p) == [], "最上位が配列でも受ける"
    finally:
        shutil.rmtree(d)


def test_load_decisions_drops_only_bad_rows():
    """1行の書き損じで全部を失わない。"""
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "decisions.json")
        write(p, {"decisions": [
            {"date": "2026-09-02", "code": "1234", "action": "buy",
             "reason": "25MA到達", "price": 1000.0},
            {"date": "8月上旬", "code": "1234"},          # 日付が読めない
            {"code": "1234"},                             # date なし
            {"date": "2026-09-03"},                       # code なし
            "not a dict",
            {"date": "2026-09-04", "code": "12340"},      # 5桁は4桁に畳む
        ]})
        got = sc.load_decisions(p)
        assert [r["code"] for r in got] == ["1234", "1234"]
        assert got[0]["date"] == "2026-09-02" and got[0]["price"] == 1000.0
        assert got[1]["action"] == "buy", "action 省略時の既定"
        assert got[1]["price"] is None
    finally:
        shutil.rmtree(d)


def test_load_decisions_is_sorted():
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "decisions.json")
        write(p, {"decisions": [{"date": "2026-09-04", "code": "9999"},
                                {"date": "2026-09-02", "code": "1234"}]})
        assert [r["date"] for r in sc.load_decisions(p)] == ["2026-09-02", "2026-09-04"]
    finally:
        shutil.rmtree(d)


def test_repo_decisions_file_is_valid():
    """リポジトリに置いた雛形がそのまま読めること。"""
    p = os.path.join(os.path.dirname(HERE), "docs", "data", "decisions.json")
    assert os.path.exists(p), "docs/data/decisions.json を置いておく（GitHub上で編集するため）"
    assert sc.load_decisions(p) == []


# ----------------------------------------------------------------------
# settle_decision: 5営業日後の突合
# ----------------------------------------------------------------------
def test_settle_done():
    dec = {"date": "2026-09-02", "code": "1234", "price": 102.0, "action": "buy"}
    o = sc.settle_decision(dec, DATES, CLOSES, TARGET, hold=5)
    assert o["status"] == "done"
    assert o["base_date"] == "2026-09-02" and o["entry"] == 102.0
    assert o["exit_date"] == "2026-09-09" and o["exit"] == 112.0      # 添字1+5=6
    assert abs(o["pnl_pct"] - (112.0 / 102.0 - 1) * 100) < 1e-9


def test_settle_pending_when_not_enough_bars():
    """5営業日未経過は「経過待ち」。残り日数も出す。"""
    dec = {"date": "2026-09-09", "code": "1234", "price": 112.0}
    o = sc.settle_decision(dec, DATES, CLOSES, TARGET, hold=5)
    assert o["status"] == "pending" and o["left"] == 3     # 残り 09/10,09/11 の先3日
    assert o["pnl_pct"] is None, "経過待ちで 0% と出さない"
    last = sc.settle_decision({"date": TARGET, "code": "1234", "price": 115.0},
                              DATES, CLOSES, TARGET, hold=5)
    assert last["status"] == "pending" and last["left"] == 5


def test_settle_holiday_decision_date_rolls_forward():
    """判断日が休場なら、その日以降で最初の営業日を起点にする。"""
    dec = {"date": "2026-09-05", "code": "1234", "price": 100.0}      # 土曜
    o = sc.settle_decision(dec, DATES, CLOSES, TARGET, hold=3)
    assert o["base_date"] == "2026-09-07"
    assert o["exit_date"] == "2026-09-10"


def test_settle_price_omitted_uses_close():
    dec = {"date": "2026-09-02", "code": "1234", "price": None}
    o = sc.settle_decision(dec, DATES, CLOSES, TARGET, hold=5)
    assert o["entry"] == 102.0 and o["status"] == "done"


def test_settle_unknown_cases():
    dec = {"date": "2026-09-02", "code": "1234", "price": 100.0}
    assert sc.settle_decision(dec, [], [], TARGET)["status"] == "unknown"
    future = {"date": "2027-01-01", "code": "1234", "price": 100.0}
    o = sc.settle_decision(future, DATES, CLOSES, TARGET)
    assert o["status"] == "unknown" and "範囲外" in o["reason"]
    zero = {"date": "2026-09-02", "code": "1234", "price": 0.0}
    assert sc.settle_decision(zero, DATES, CLOSES, TARGET)["status"] == "unknown"
    holed = list(CLOSES)
    holed[6] = None
    o2 = sc.settle_decision(dec, DATES, holed, TARGET, hold=5)
    assert o2["status"] == "unknown" and "終値" in o2["reason"]


# ----------------------------------------------------------------------
# settle_decisions: 候補に無いコードの追加取得
# ----------------------------------------------------------------------
class CountingJQ:
    def __init__(self, bars_by_code):
        self.bars_by_code = bars_by_code
        self.calls = []

    def get(self, path, params=None):
        p = dict(params or {})
        self.calls.append((path, p))
        return list(self.bars_by_code.get(p.get("code"), []))


def test_settle_decisions_reuses_candidate_series_without_extra_api():
    sc.REQ["n"] = 0
    jq = CountingJQ({})
    series = {"1234": (DATES, CLOSES)}
    out = sc.settle_decisions(jq, [{"date": "2026-09-02", "code": "1234",
                                    "action": "buy", "reason": "", "price": 102.0}],
                              series, TARGET)
    assert out[0]["outcome"]["status"] == "done"
    assert jq.calls == [], "当日の候補に居るコードは追加取得しない"
    assert sc.REQ["n"] == 0


def test_settle_decisions_fetches_missing_code_once():
    sc.REQ["n"] = 0
    rows = [{"Date": d, "AdjC": c} for d, c in zip(DATES, CLOSES)]
    jq = CountingJQ({"9999": rows})
    decs = [{"date": "2026-09-02", "code": "9999", "action": "buy", "reason": "", "price": 102.0},
            {"date": "2026-09-03", "code": "9999", "action": "skip", "reason": "", "price": None}]
    out = sc.settle_decisions(jq, decs, {}, TARGET)
    assert len(jq.calls) == 1, "同じコードは1回だけ取る"
    assert sc.REQ["n"] == 1
    assert out[0]["outcome"]["status"] == "done"
    assert out[1]["outcome"]["entry"] == 101.0


def test_settle_decisions_respects_fetch_cap_and_says_so(capsys=None):
    sc.REQ["n"] = 0
    rows = [{"Date": d, "AdjC": c} for d, c in zip(DATES, CLOSES)]
    codes = [f"{9000 + i}" for i in range(5)]
    jq = CountingJQ({c: rows for c in codes})
    decs = [{"date": "2026-09-02", "code": c, "action": "buy", "reason": "", "price": 100.0}
            for c in codes]
    out = sc.settle_decisions(jq, decs, {}, TARGET, max_fetch=2)
    assert len(jq.calls) == 2
    unknown = [o for o in out if o["outcome"]["status"] == "unknown"]
    assert len(unknown) == 3
    assert "上限" in unknown[0]["outcome"]["reason"], "打ち切ったことを黙って隠さない"


def test_settle_decisions_survives_fetch_error():
    class Boom(CountingJQ):
        def get(self, path, params=None):
            raise RuntimeError("HTTP 500")
    out = sc.settle_decisions(Boom({}), [{"date": "2026-09-02", "code": "9999",
                                          "action": "buy", "reason": "", "price": 100.0}],
                              {}, TARGET)
    assert out[0]["outcome"]["status"] == "unknown"


def test_code_mismatch_is_reported_not_crashed():
    """判断ログのコードが当日の候補に無くても落ちない（V2-7 コード不一致）。"""
    out = sc.settle_decisions(CountingJQ({}), [{"date": "2026-09-02", "code": "0000",
                                                "action": "buy", "reason": "", "price": 1.0}],
                              {"1234": (DATES, CLOSES)}, TARGET)
    assert out[0]["outcome"]["status"] == "unknown"


# ----------------------------------------------------------------------
# E2E: 画面に出る
# ----------------------------------------------------------------------
def test_decision_ui_present_and_empty_state():
    recs, _ = build_records()
    html, latest = render_to_tmp(recs)
    for must in ('id="declog"', "function secDecision(code, disp)", "function wireDecision()",
                 'id="d-copy"', 'id="d-reason"', 'id="d-price"',
                 "JSON行をコピー", "docs/data/decisions.json", "判断ログ"):
        assert must in html, must
    assert "const DECISIONS = [];" in html
    assert latest["decisions"] == []
    assert "まだ1件も記録が無い" in html
    assert "システムの推奨ではない" in html


def test_decision_outcomes_reach_the_page():
    recs, _ = build_records()
    decs = sc.settle_decisions(
        CountingJQ({}),
        [{"date": "2026-09-02", "code": "1301", "action": "buy", "reason": "25MA到達＋枯れ比18%",
          "price": 102.0},
         {"date": "2026-09-10", "code": "1301", "action": "skip", "reason": "決算跨ぎ",
          "price": 111.0}],
        {"1301": (DATES, CLOSES)}, TARGET)
    d = tempfile.mkdtemp()
    try:
        sc.render(TARGET, recs, dict(sc.DEFAULT_CRITERIA), docs=d, decisions=decs)
        with open(os.path.join(d, "index.html"), encoding="utf-8") as f:
            html = f.read()
        with open(os.path.join(d, "data", "latest.json"), encoding="utf-8") as f:
            latest = json.load(f)
    finally:
        shutil.rmtree(d)
    assert "25MA到達＋枯れ比18%" in html and "決算跨ぎ" in html
    assert len(latest["decisions"]) == 2
    assert latest["decisions"][0]["outcome"]["status"] == "done"
    assert latest["decisions"][1]["outcome"]["status"] == "pending"
    assert "経過待ち（あと' + o.left + '営業日）" in html, "経過待ちの表示分岐がある"


def test_decision_reason_is_escaped():
    recs, _ = build_records()
    decs = [{"date": "2026-09-02", "code": "1301", "action": "buy",
             "reason": "</script><b>x</b>", "price": 100.0,
             "outcome": {"status": "unknown", "reason": "", "base_date": None, "entry": None,
                         "exit_date": None, "exit": None, "pnl_pct": None, "left": None}}]
    d = tempfile.mkdtemp()
    try:
        sc.render(TARGET, recs, dict(sc.DEFAULT_CRITERIA), docs=d, decisions=decs)
        with open(os.path.join(d, "index.html"), encoding="utf-8") as f:
            html = f.read()
    finally:
        shutil.rmtree(d)
    assert html.count("</script>") == 3
    assert "\\u003c/script\\u003e" in html


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
