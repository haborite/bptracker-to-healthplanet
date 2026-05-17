# bptracker-to-healthplanet

[aadhk Blood Pressure Tracker](https://play.google.com/store/apps/details?id=com.aadhk.lite.bptracker) アプリのエクスポートデータ（SQLite3）を [Health Planet](https://www.healthplanet.jp/) の血圧入力画面へ自動転記するスクリプト。

Health Planet に API がないため Playwright によるブラウザ自動操作を使用する。

## 動作環境

- Python 3.12+
- Windows / macOS / Linux

## セットアップ

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## 設定

`login_data/health_planet.json` に Health Planet のログイン情報を記載する。

```json
{
    "id": "your_id",
    "password": "your_password"
}
```

## データ配置

bptracker からエクスポートした `.db` ファイルを `bpdata/` に置く。複数ファイル可。同一日時のレコードは重複除外される。

```
bpdata/
  bptracker_2026_01.db
  bptracker_2026_05.db
```

## 使い方

```bash
# 内容確認のみ（送信しない）
python upload.py --dry-run

# 全レコードを送信（ブラウザ表示あり）
python upload.py

# 期間指定
python upload.py --from-date 20260101 --to-date 20260131

# バックグラウンド実行（ブラウザ非表示）
python upload.py --headless

# 送信間隔を変更（デフォルト 1.5 秒）
python upload.py --delay 2.0
```

### オプション一覧

| オプション | 説明 |
|---|---|
| `--dry-run` | レコードを表示するだけで送信しない |
| `--headless` | ブラウザを非表示で実行 |
| `--delay DELAY` | エントリ間の待機秒数（デフォルト: 1.5） |
| `--from-date YYYYMMDD` | この日付以降のレコードのみ処理 |
| `--to-date YYYYMMDD` | この日付以前のレコードのみ処理 |

## 転記される項目

bptracker の `tranx` テーブルから以下を読み込む。

| DB カラム | Health Planet 項目 |
|---|---|
| `tranxDate` + `tranxTime` | 測定日時 |
| `sys` | 最高血圧（mmHg） |
| `dia` | 最低血圧（mmHg） |
| `pulse` | 脈拍（回/分） |

`sys`・`dia`・`pulse` がすべて 0 のレコードはスキップされる。
