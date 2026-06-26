# wbagger-screener

J-Quants API を使った日本株「初動スクリーニング」。GitHub Actions が毎朝（平日8:00 JST）自動実行し、結果を `docs/` に出力。GitHub Pages でブラウザ閲覧できる。

## 仕組み
1. Actions が `screener.py` を実行
2. J-Quants 認証 → 東証グロースの前日データ取得
3. 値上がり率 / 出来高急増 / S高 / 移動平均(パーフェクトオーダー) / MACD / 時価総額 / ファンダ / タブー / 信用残を計算
4. `docs/index.html` と `docs/data/latest.json` を生成しコミット
5. GitHub Pages が公開

## セットアップ
1. リポジトリ Secrets に登録（Settings → Secrets and variables → Actions）
   - `JQUANTS_MAILADDRESS`
   - `JQUANTS_PASSWORD`
2. GitHub Pages を有効化（Settings → Pages → Source: `main` / フォルダ `/docs`）
3. Actions タブ → `screen` → **Run workflow** で手動実行（初回テスト）
4. 公開URL（例）: `https://<ユーザー名>.github.io/wbagger-screener/`

## 閾値の調整
`criteria.yaml` を編集（時価総額レンジ・出来高倍率・利益率など）。

## 制約
- 無料プランは12週遅延のため Light 以上が必要（前営業日データ）
- 材料・テーマ・開示の中身は J-Quants 非配信 → TDnet/EDINET で別途確認
- EOD（日次）ベース。ザラ場の板・歩み値は対象外
- フィールド名は J-Quants 公式リファレンスで要確認（仕様変更時は `screener.py` を調整）

## 免責
本ツールの抽出は機械的処理であり投資助言ではない。最終判断は自己責任。
