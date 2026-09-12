#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""main() の中断条件（無人実行で壊れたページを公開しないための番人）。ネット不要。

`python3 -m pytest tests/ -q` でも `python3 tests/test_screener_main.py` でも実行可。
"""

import os
import sys
import shutil
import datetime as dt
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import screener as sc                     # noqa: E402
from test_screener_render import synth_bars, TARGET   # noqa: E402


class ScriptedJQ:
    """日付一括・銘柄別・master を返すモック。fail_paths に入れた path は例外を投げる。"""

    def __init__(self, master, bars_by_code, fail_paths=()):
        self.master = master
        self.bars_by_code = bars_by_code
        self.fail_paths = set(fail_paths)
        self.by_date = {}
        for rows in bars_by_code.values():
            for r in rows:
                self.by_date.setdefault(r["Date"], []).append(r)

    def get(self, path, params=None):
        p = dict(params or {})
        if path in self.fail_paths:
            raise RuntimeError("HTTP 403")
        if path == "/equities/master":
            return list(self.master)
        if path == "/equities/bars/daily":
            if "code" in p:
                return list(self.bars_by_code.get(p["code"], []))
            return list(self.by_date.get(p.get("date"), []))
        if path == "/fins/summary":
            return []
        return []


def run_main(jq, crit_over=None, docs=None):
    """main() を差し替え済みの部品で実行して戻り値を得る。"""
    orig_jq, orig_crit, orig_docs, orig_env = sc.JQuants, sc.load_criteria, sc.DOCS_DIR, \
        os.environ.get("JQUANTS_API_KEY")
    crit = dict(sc.DEFAULT_CRITERIA)
    crit.update(crit_over or {})
    sc.REQ["n"] = 0
    try:
        os.environ["JQUANTS_API_KEY"] = "dummy"
        sc.JQuants = lambda *a, **k: jq
        sc.load_criteria = lambda: crit
        if docs:
            sc.DOCS_DIR = docs
        return sc.main()
    finally:
        sc.JQuants, sc.load_criteria, sc.DOCS_DIR = orig_jq, orig_crit, orig_docs
        if orig_env is None:
            os.environ.pop("JQUANTS_API_KEY", None)
        else:
            os.environ["JQUANTS_API_KEY"] = orig_env


def _bars():
    return {"13010": synth_bars("13010", n=40, sh_offset=3),
            "338A0": synth_bars("338A0", n=40, sh_offset=0)}


def _master(mkt="グロース"):
    return [{"Code": "13010", "CoName": "あ", "MktNm": mkt, "S33Nm": "情報・通信業"},
            {"Code": "338A0", "CoName": "い", "MktNm": mkt, "S33Nm": "電気機器"}]


def test_happy_path_writes_page():
    d = tempfile.mkdtemp()
    try:
        rc = run_main(ScriptedJQ(_master(), _bars()), docs=d)
        assert rc == 0
        assert os.path.exists(os.path.join(d, "index.html"))
        assert os.path.exists(os.path.join(d, "data", "latest.json"))
    finally:
        shutil.rmtree(d)


def test_empty_universe_aborts_before_scanning():
    """master に一致が0件なら、全市場へフォールバックせずに止まる。"""
    d = tempfile.mkdtemp()
    try:
        jq = ScriptedJQ(_master("プライム"), _bars())
        rc = run_main(jq, {"markets": ["グロース", "スタンダード"]}, docs=d)
        assert rc == 1
        assert not os.path.exists(os.path.join(d, "index.html")), \
            "止めた以上ページは書かない（前回のページを残す）"
        assert sc.REQ["n"] < 10, f"S高探索まで進んでいる: {sc.REQ['n']}回"
    finally:
        shutil.rmtree(d)


def test_majority_analysis_failure_aborts_without_publishing():
    """銘柄別の取得が軒並み落ちたら、短い一覧を公開せず中断する。"""
    d = tempfile.mkdtemp()
    try:
        bars = _bars()
        jq = ScriptedJQ(_master(), bars)
        # 銘柄別 bars だけ落とす（日付一括は生かす）ため get を包む
        base_get = jq.get

        def get(path, params=None):
            if path == "/equities/bars/daily" and "code" in (params or {}):
                raise RuntimeError("HTTP 500")
            return base_get(path, params)
        jq.get = get
        rc = run_main(jq, docs=d)
        assert rc == 1
        assert not os.path.exists(os.path.join(d, "index.html"))
    finally:
        shutil.rmtree(d)


def test_single_failure_still_publishes_and_reports_count():
    """1件だけ落ちた日は、件数を明示したうえで公開する。"""
    d = tempfile.mkdtemp()
    try:
        bars = _bars()
        bars["99970"] = synth_bars("99970", n=40, sh_offset=1)
        bars["47770"] = synth_bars("47770", n=40, sh_offset=2)
        master = _master() + [
            {"Code": "99970", "CoName": "う", "MktNm": "グロース", "S33Nm": "小売業"},
            {"Code": "47770", "CoName": "え", "MktNm": "グロース", "S33Nm": "小売業"}]
        jq = ScriptedJQ(master, bars)
        base_get = jq.get

        def get(path, params=None):
            p = params or {}
            if path == "/equities/bars/daily" and p.get("code") == "47770":
                raise RuntimeError("HTTP 403")
            return base_get(path, p)
        jq.get = get
        rc = run_main(jq, docs=d)
        assert rc == 0
        with open(os.path.join(d, "index.html"), encoding="utf-8") as f:
            html = f.read()
        assert "解析失敗 1件" in html
    finally:
        shutil.rmtree(d)


def test_request_count_is_measured():
    """V3「APIリクエスト数を実測し報告」が動くこと。走査ぶんが窓の日数に比例する。"""
    d = tempfile.mkdtemp()
    try:
        run_main(ScriptedJQ(_master(), _bars()), {"sh_window": 5}, docs=d)
        n5 = sc.REQ["n"]
        run_main(ScriptedJQ(_master(), _bars()), {"sh_window": 10}, docs=d)
        n10 = sc.REQ["n"]
        assert n10 - n5 == 5, f"窓を5日広げたら5リクエスト増えるはず: {n5} -> {n10}"
    finally:
        shutil.rmtree(d)


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
