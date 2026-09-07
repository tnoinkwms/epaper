# epaper

Waveshare 3.7inch e-Paper (G / 4色) を壁掛けの表示板として常時運用するための一式。

ブラウザからテキスト・画像・手書きを投稿すると、ESP32 がそれを取りに来て
パネルを書き換える。サーバは Railway に常設し、ESP32 はインターネット越しに
そこを見るので、ルータのポート開放も固定 IP も要らない。

```
  ブラウザ ──HTTPS──> Railway (server.py / gunicorn) <──HTTPS── ESP32 ──SPI──> e-Paper
                          └ Volume /data にフレームを保存
```

## 構成

| ファイル | 役割 |
|---|---|
| [`server.py`](server.py) | 投稿用 Web UI とフレーム配信。画像をパネル形式(2bit/px, 24,960 バイト)に変換する |
| [`ESP32.ino`](ESP32.ino) | 端末側ファームウェア。ロングポーリングで更新を待ち、変化があれば描画する |
| [`Dockerfile`](Dockerfile) / [`railway.json`](railway.json) | Railway 常設用のビルド・デプロイ設定 |
| [`DEPLOY.md`](DEPLOY.md) | **デプロイ手順はこちら** |

## 使う

サーバの立て方・環境変数・ESP32 への書き込みは [DEPLOY.md](DEPLOY.md) を参照。

ローカルで動かすだけなら:

```bash
uv run server.py        # 依存も Python 本体も uv が用意する
```

表示された URL をブラウザで開き、`ESP32.ino` の `SERVER_BASE` を
`http://<PCのIP>:8000` にして書き込めば LAN 内で完結する
(スキームを見て TLS の有無が切り替わる)。

## 設計上のポイント

- **パネル寿命の保護はファームウェア側で強制する**。更新間隔の下限 3 分、
  焼き付き防止の 23 時間ごとの強制更新、更新時以外はパネルの電源を落とす。
  サーバを差し替えても、この保護は消えない。
- **e-Paper は表示保持型**。したがって ESP32 の再起動もサーバの再デプロイも
  利用者からは見えない。計画再起動(7日)やヒープ枯渇時の再起動を安全に使える。
- **フレームは version(SHA-1 の先頭 16 桁)で同一性を判定する**。サーバ側は
  Volume にフレームを保存して再起動を跨いで version を保つので、再デプロイで
  余計な書き換えが起きない。

## ライセンス

同梱の Noto Sans JP は SIL Open Font License 1.1
([`Noto_Sans_JP/OFL.txt`](Noto_Sans_JP/OFL.txt))。
