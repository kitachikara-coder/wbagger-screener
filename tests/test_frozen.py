#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""凍結事項のデグレ検知（依頼書 0節 / V1）。

指標計算関数と JQuants クライアントは **1文字も変えない** 約束なので、
AST でソース断片を切り出して SHA256 を固定する。ハッシュが変わったら、
「本当に変えてよい変更か」を人が判断してから、ここの値を更新すること。
`git diff --stat screener.py` を見るより確実（周囲の行がずれても検知できる）。

`python3 -m pytest tests/ -q` でも `python3 tests/test_frozen.py` でも実行可。
"""

import ast
import os
import sys
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

SCREENER = os.path.join(ROOT, "screener.py")

# 2026-09-12 時点のソース。モックで検証済みのため以後は凍結。
FROZEN = {
    "sma":             "9ee76d4f9dcf5b40",
    "sma_series":      "9983f0e355b1fdd4",
    "ema_series":      "84028106b15812b6",
    "macd_series":     "0115a1639f0d9ede",
    "rci_series":      "e1355b8e21031f4f",
    "build_series":    "d8970c3501665fef",
    "resample_weekly": "2450b9ec617a7f53",
    "JQuants":         "42dde46d021581f0",
}


def source_hashes(path):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    lines = src.splitlines(True)
    out = {}
    for node in ast.parse(src).body:
        name = getattr(node, "name", None)
        if name in FROZEN:
            seg = "".join(lines[node.lineno - 1:node.end_lineno])
            out[name] = hashlib.sha256(seg.encode("utf-8")).hexdigest()[:16]
    return out


def test_frozen_functions_unchanged():
    got = source_hashes(SCREENER)
    missing = sorted(set(FROZEN) - set(got))
    assert not missing, f"凍結対象が screener.py から消えている: {missing}"
    changed = {k: (FROZEN[k], got[k]) for k in FROZEN if FROZEN[k] != got[k]}
    assert not changed, f"凍結関数が変更されている: {changed}"


def test_backtests_do_not_import_screener():
    """backtest.py / backtest_dip.py は screener.py と独立（検証の一貫性を守るため）。"""
    for name in ("backtest.py", "backtest_dip.py"):
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            src = f.read()
        assert "import screener" not in src, f"{name} が screener.py に依存している"


def test_bracket_rules_are_intact():
    """backtest_dip.bracket() の 3つの約束（両触れは損切り優先 / 往復0.25% / 翌営業日始値）。"""
    path = os.path.join(ROOT, "backtest_dip.py")
    if not os.path.exists(path):
        return
    sys.path.insert(0, ROOT)
    import backtest_dip as bd
    assert (bd.TP, bd.SL, bd.COST, bd.HOLD) == (0.05, -0.07, 0.0025, 5)
    n = 8
    adjo = [100.0] * n
    adjh = [110.0] * n          # 同日に +5% も -7% も触れる
    adjl = [90.0] * n
    adjc = [100.0] * n
    assert bd.bracket(adjo, adjh, adjl, adjc, 1) == (bd.SL - bd.COST, "SL"), "両触れは損切り優先"
    # 利確だけ触れる
    assert bd.bracket(adjo, [106.0] * n, [99.0] * n, adjc, 1) == (bd.TP - bd.COST, "TP")
    # どちらも触れずに時間切れ → 終値決済（往復コスト控除）
    pnl, kind = bd.bracket(adjo, [101.0] * n, [99.0] * n, [102.0] * n, 1)
    assert kind == "TIME" and abs(pnl - (0.02 - bd.COST)) < 1e-12


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
