# e-Paper 配信サーバ (Railway 常設用)
FROM python:3.12-slim

# 絵文字フォント。macOSの Apple Color Emoji はコンテナに存在しないので、
# Noto Color Emoji を入れる。これが無いと絵文字は黙って捨てられる。
# 日本語フォントは同梱の Noto Sans JP を使うので、追加インストールは不要。
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依存だけ先に入れる。server.py を書き換えただけの再デプロイでは
# このレイヤがキャッシュされ、ビルドが速い。
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Volume のマウント先と揃えること (DEPLOY.md 参照)
ENV DATA_DIR=/data
ENV PORT=8000
EXPOSE 8000

# -w 1 は必須。
#   フレームはプロセス内のメモリに持っているので、ワーカーを増やすと
#   「投稿を受けたワーカー」と「/wait を握っているワーカー」が別になり、
#   更新が永久に配信されない。
# 同時接続はスレッドで捌く。ロングポーリングが1本張りっぱなしになるため、
# シングルスレッドのワーカーではブラウザからの投稿が待たされる。
# --timeout は POLL_HOLD_S(既定25秒)より十分長くしておくこと。
# シェル形式で書く。${PORT} を展開する必要があるため。
# exec を付けて、gunicorn が PID 1 を引き継ぐようにする。付けないと
# 間に挟まる sh が SIGTERM を握り潰し、再デプロイのたびに強制killされる。
CMD exec gunicorn server:app \
  --bind 0.0.0.0:${PORT:-8000} \
  --workers 1 \
  --worker-class gthread \
  --threads 16 \
  --timeout 120 \
  --graceful-timeout 30 \
  --access-logfile - \
  --error-logfile -
