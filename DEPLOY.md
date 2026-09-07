# Railway に常設する

`server.py` を Railway 上で動かしっぱなしにして、ESP32 がインターネット越しに
そこを見に行く構成。ルータのポート開放も固定IPも要らない。

```
  ブラウザ ──HTTPS──> Railway (server.py / gunicorn) <──HTTPS── ESP32 ──SPI──> e-Paper
                          └ Volume /data にフレームを保存
```

---

## 1. デプロイ

### CLI で入れる場合

```bash
brew install railway          # または npm i -g @railway/cli
railway login
cd /Users/torineshia/code/epaper
railway init                  # プロジェクトを作る
railway up                    # Dockerfile を使ってビルド & デプロイ
railway domain                # 公開ドメインを発行する
```

`railway domain` が出す `xxxx.up.railway.app` が接続先になる。あとで使う。

### GitHub 経由で入れる場合

このディレクトリをリポジトリに push し、Railway のダッシュボードで
**New Project → Deploy from GitHub repo** を選ぶ。`railway.json` と `Dockerfile`
が読まれるので、ビルド設定を手で入れる必要はない。以後は push するたびに
再デプロイされる。公開URLは **Settings → Networking → Generate Domain** で発行する。

---

## 2. 環境変数

Railway のダッシュボード **Variables** で設定する(CLI なら
`railway variables --set "EPD_PASS=..."`)。

| 変数 | 必須 | 説明 |
|---|---|---|
| `EPD_PASS` | **必須** | Basic認証のパスワード。**設定しないと、URLを知っている人全員が部屋の壁に好きな画像を出せる** |
| `EPD_USER` | 任意 | Basic認証のユーザ名。既定 `epd` |
| `DATA_DIR` | 任意 | フレームの保存先。Dockerfile で `/data` 済み |
| `POLL_HOLD_S` | 任意 | `/wait` が待つ最大秒数。既定 25。伸ばすときは**先に ESP32 の `POLL_TIMEOUT_MS` を伸ばす** |
| `PORT` | — | Railway が自動で入れる。触らない |

パスワードは適当に長いものを。例:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

---

## 3. Volume を付ける (推奨)

ダッシュボードでサービスを選び、**Settings → Volumes → Add Volume**、
マウントパスを `/data` にする。

無くても動くが、付けておくと再デプロイやコンテナ再起動を跨いで
「今出している画像」が保持される。付けないと、再デプロイのたびに
サーバ側のフレームが消え、次に画像を投稿したときに ESP32 から見て
version が変わったように見える(= 余計なフル書き換えが1回増える)。

---

## 4. ESP32 に書き込む

`ESP32.ino` の先頭2箇所を、発行されたドメインと設定したパスワードに書き換える。

```cpp
static const char* SERVER_BASE = "https://xxxx.up.railway.app";  // 末尾スラッシュ無し
static const char* DEVICE_USER = "epd";
static const char* DEVICE_PASS = "ここに EPD_PASS と同じ文字列";
```

必要なライブラリは従来どおり **WiFiManager (tzapu)**。`WiFiClientSecure` は
ESP32 のコアに入っているので追加インストールは不要。

書き込んだら、シリアルモニタ(115200)に

```
server: https://xxxx.up.railway.app (tls=1, auth=1)
wifi: <SSID>  ip=192.168.x.x
```

と出れば接続先の設定は合っている。`401:` が出たらパスワードの不一致。

---

## 5. 動作確認

```bash
BASE=https://xxxx.up.railway.app
curl -s $BASE/healthz                       # 認証不要。ok version=... が返る
curl -s -u epd:$EPD_PASS $BASE/version
curl -s -u epd:$EPD_PASS -o /dev/null -w '%{http_code} %{size_download}\n' $BASE/frame.bin
```

`frame.bin` は 24960 バイト。ブラウザで `$BASE/` を開くと Basic認証を訊かれ、
通ればいつもの投稿画面が出る。

---

## 6. 運用メモ

- **App Sleeping は切ってある** (`railway.json` の `sleepApplication: false`)。
  ESP32 が常時ロングポーリングしているので実際には眠らないが、
  眠らせると復帰の数秒間 502 が返り、そのぶん反映が遅れる。

- **ワーカーは1個で固定** (`Dockerfile` の `--workers 1`)。フレームはプロセス内の
  メモリに持っているので、増やすと投稿を受けたワーカーと `/wait` を握っている
  ワーカーが別になり、更新が配信されなくなる。負荷はスレッドで捌く。

- **再デプロイ中は数十秒 502 になる**。ESP32 は指数バックオフ(最大5分)で
  待つだけで、その間 e-Paper の表示はそのまま残る。放置してよい。

- **費用**。ほぼ待っているだけのプロセスなので CPU はほぼ 0、メモリも
  100MB 前後。Hobby プランの範囲に収まる想定。

- **ログ**は `railway logs`、またはダッシュボードの Deployments から見る。
  投稿のたびに `published: 24960 bytes, version=...` が出る。

- **フォント**。ヒラギノは macOS 専用なので、コンテナでは同梱の Noto Sans JP が
  使われる(字形が少し変わる)。絵文字は Dockerfile で入れた Noto Color Emoji。

- **ローカルで動かしたいとき**は今までどおり `uv run server.py`。
  その場合は `ESP32.ino` の `SERVER_BASE` を `http://<PCのIP>:8000` に戻せば、
  TLS 無しでそのまま繋がる(スキームを見て自動で切り替わる)。
