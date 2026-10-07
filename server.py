#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "flask>=3.0",
#     "pillow>=10.0",
# ]
# ///
"""
server.py - e-Paper 3.7inch (G) 用フレーム配信サーバ

ローカル(開発時):
  uv run server.py
  -> http://<このPCのIP>:8000/ をブラウザで開く
  依存関係とPython本体はuvが自動で用意する。事前のpip installは不要。

Railway(常設運用):
  Dockerfile でビルドされ、gunicorn から `server:app` として読み込まれる。
  __main__ ブロックは実行されないので、設定は全て環境変数で渡す。
  詳細は DEPLOY.md を参照。

環境変数:
  PORT          待ち受けポート。Railwayが自動で渡してくる (既定 8000)
  DATA_DIR      フレームの保存先ディレクトリ (既定 ./data)
  EPD_USER      Basic認証のユーザ名 (既定 epd)
  EPD_PASS      Basic認証のパスワード。空なら認証なし
  POLL_HOLD_S   /wait が変化を待つ最大秒数 (既定 25)

エンドポイント:
  GET  /            設定用のWebフォーム
  POST /text        テキストを画像化して登録
  POST /image       画像ファイルを登録
  POST /draw        手書きキャンバスのPNGを登録
  GET  /preview.png 現在のフレームのプレビュー画像
  GET  /wait        ロングポーリング。変化があるまで最大 POLL_HOLD_S 秒待つ
  GET  /version     現在のフレームのハッシュ(ESP32が差分判定に使う)
  GET  /frame.bin   24,960バイトの生フレームデータ
                    X-Frame-Version ヘッダに、その中身に対応するversionを載せる
  GET  /healthz     死活監視。Railwayのヘルスチェック用。認証は不要
"""

import io
import os
import re
import hmac
import time
import math
import hashlib
import threading
import unicodedata
from pathlib import Path
from flask import Flask, request, send_file, Response, render_template_string

from PIL import Image, ImageDraw, ImageFont

# =====================================================================
# 設定 - ここは自分の環境に合わせて変更する
# =====================================================================

# パネルのネイティブ解像度。EPD_3in7g.h の値と一致させること。
PANEL_W = 240          # EPD_3IN7G_WIDTH
PANEL_H = 416          # EPD_3IN7G_HEIGHT

# パネルは縦置きで使う。描画キャンバスはパネルのネイティブ解像度と同じ。
# テキストも画像も全てこの縦長キャンバスに描くので、回転は発生しない。
CANVAS_W = PANEL_W     # 240
CANVAS_H = PANEL_H     # 416

# キャンバス -> ネイティブバッファ への回転量。
# 縦置きなので通常は0。表示が上下逆さまになったら 180 にする。
ROTATE = 0

# 2bitのカラーコード。EPD_3in7g.h の #define を必ず確認して合わせること。
# 典型的には BLACK=0, WHITE=1, YELLOW=2, RED=3
CODE_BLACK  = 0
CODE_WHITE  = 1
CODE_YELLOW = 2
CODE_RED    = 3

# パレットのRGB値。実機の発色に合わせて調整する余地がある。
# 特に赤は鮮やかな赤ではなく煉瓦色に近いので、写真の見た目を詰めるなら実測推奨。
PALETTE = [
    ((255, 255, 255), CODE_WHITE),
    ((0,   0,   0),   CODE_BLACK),
    ((230, 190, 0),   CODE_YELLOW),
    ((190, 45,  40),  CODE_RED),
]

# 白黒2値用。パネルは4色のままなので、フレーム形式もサイズも変わらない。
# 使う色をこの2つに絞るだけ。写真は黄・赤が乗らない分きれいに出ることが多い。
PALETTE_BW = [
    ((255, 255, 255), CODE_WHITE),
    ((0,   0,   0),   CODE_BLACK),
]

# 日本語フォント。上から順に探し、最初に見つかったものを使う。
#
# ヒラギノ角ゴシックはmacOS標準の日本語フォントで、ひらがなの字形が
# 見慣れた形になる。ただしmacOSにバンドルされているものなので、
# Mac以外(VPS等)へ持ち出すことはできない。同梱のNoto Sans JPはその保険で、
# パスを直書きして存在しない環境で500になるのを防ぐ意味もある。
HERE = Path(__file__).resolve().parent
FONT_CANDIDATES = [
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",   # HiraginoSans-W3 (index 0)
    str(HERE / "Noto_Sans_JP" / "static" / "NotoSansJP-Regular.ttf"),
]


def _resolve_font(candidates):
    """最初に実在するフォントのパスを返す。見つからなければ None。

    macOSはファイル名をNFDで持つことがあり、「ヒラギノ」のような濁点を含む
    名前はNFCのリテラルと一致しない場合がある。両方の正規化形で試す。
    """
    for cand in candidates:
        for form in ("NFC", "NFD"):
            q = Path(unicodedata.normalize(form, cand))
            if q.exists():
                return str(q)
    return None


# 全滅したときは最後の候補を入れておく。起動時ではなく描画時に、
# どのパスで失敗したかが分かるエラーを出させる。
FONT_JP = _resolve_font(FONT_CANDIDATES) or FONT_CANDIDATES[-1]

# 絵文字フォント。カラー絵文字はビットマップ内蔵のことが多く、その場合は
# 埋め込まれているサイズちょうどでしか開けない(それ以外は OSError)。
# サイズはフォントごとに違うので、パスと希望サイズを組にして持ち、
# 起動時に実際に開いてみて確定させる。
#   Apple Color Emoji : macOS標準。160px
#   Noto Color Emoji  : Dockerfileで fonts-noto-color-emoji を入れる。109px
#                       (ディストリによってパスが違うので、最後にglobで探す)
FONT_EMOJI_CANDIDATES = [
    ("/System/Library/Fonts/Apple Color Emoji.ttc", 160),
    ("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", 109),
    ("/usr/share/fonts/opentype/noto/NotoColorEmoji.ttf", 109),
]
EMOJI_SEARCH_DIRS = ["/usr/share/fonts", "/usr/local/share/fonts"]


def _probe_emoji_size(path, preferred):
    """そのフォントを実際に開けるサイズを返す。全滅なら0。

    パスが存在するだけでは足りない。ビットマップフォントは想定外の
    サイズだと OSError("invalid pixel size") で落ちるので、開けることまで
    起動時に確認しておく。描画のたびに例外で絵文字が消えるより分かりやすい。
    """
    for size in (preferred, 109, 128, 136, 160):
        try:
            ImageFont.truetype(path, size)
            return size
        except Exception:
            continue
    return 0


def _resolve_emoji_font():
    cands = list(FONT_EMOJI_CANDIDATES)
    for d in EMOJI_SEARCH_DIRS:
        for f in sorted(Path(d).glob("**/NotoColorEmoji*.tt[fc]")):
            cands.append((str(f), 109))
    for path, size in cands:
        if not Path(path).exists():
            continue
        probed = _probe_emoji_size(path, size)
        if probed:
            return path, probed
    return None, 0


# 見つからなければNone。その場合、絵文字は描かずに落とす(本文は出る)。
FONT_EMOJI, EMOJI_NATIVE_SIZE = _resolve_emoji_font()

# =====================================================================
# 運用設定 - 環境変数で上書きする。Railwayではダッシュボードの Variables で設定
# =====================================================================

# Railwayは待ち受けポートを PORT で渡してくる。決め打ちにすると疎通しない。
PORT = int(os.environ.get("PORT", "8000"))

# フレームの保存先。Railwayでは /data にVolumeをマウントしておくと、
# 再デプロイやコンテナ再起動を跨いで「今出している画像」が残る。
# Volumeが無くても動作はする。その場合、再起動でサーバ側のフレームは消えるが、
# e-Paperは表示保持型なので実機の画面はそのまま残る。
DATA_DIR     = Path(os.environ.get("DATA_DIR", str(HERE / "data")))
FRAME_FILE   = DATA_DIR / "frame.bin"
PREVIEW_FILE = DATA_DIR / "preview.png"

# 1フレームの正しいバイト数。復元時の検証に使う。
FRAME_BYTES = (PANEL_W // 4) * PANEL_H

# /wait が変化を待つ最大秒数。ESP32側の POLL_TIMEOUT_MS より必ず短くすること。
POLL_HOLD_S = float(os.environ.get("POLL_HOLD_S", "25"))

# Basic認証。EPD_PASS が空だと認証なし(LAN内だけで使うとき用)。
# インターネットに常設するなら必ず設定する。無いと、URLを知っている人全員が
# 部屋の壁に好きな画像を出せてしまう。
EPD_USER = os.environ.get("EPD_USER", "epd")
EPD_PASS = os.environ.get("EPD_PASS", "")

# ヘルスチェックだけは認証を通さない。Railwayのヘルスチェッカーは
# 認証情報を持たないので、ここを塞ぐとデプロイが永久に成功しない。
PUBLIC_PATHS = {"/healthz"}

# =====================================================================

app = Flask(__name__)


@app.before_request
def require_auth():
    """EPD_PASS が設定されているときだけBasic認証を要求する。

    ブラウザ(Web UI)とESP32の両方が同じ認証を通る。ESP32側は
    HTTPClient::setAuthorization() で同じユーザ名・パスワードを送る。
    """
    if not EPD_PASS or request.path in PUBLIC_PATHS:
        return None
    auth = request.authorization
    if (auth and auth.type == "basic"
            and hmac.compare_digest(auth.username or "", EPD_USER)
            and hmac.compare_digest(auth.password or "", EPD_PASS)):
        return None
    return Response("authentication required\n", 401,
                    {"WWW-Authenticate": 'Basic realm="e-Paper"'})


RGB2CODE = {rgb: code for rgb, code in PALETTE}
_current_frame = None      # bytes
_current_version = "none"
_current_preview = None    # PNG bytes

# frameとversionを常にセットで読み書きするためのロック。
# これが無いと、publishの途中で /frame.bin が呼ばれたときに
# 「新しいフレーム + 古いversion」の組み合わせを返しうる。
_lock = threading.Lock()


def snapshot():
    """現在のフレームとversionを不可分に取り出す"""
    with _lock:
        return _current_frame, _current_version


# ---------------------------------------------------------------------
# フレームの永続化
#
# Railwayのコンテナは再デプロイのたびに作り直され、メモリ上の状態は消える。
# DATA_DIR に書いておき、起動時に読み直すことで、再デプロイ後も
# 「今出している画像」のversionが変わらない。これが無いと、再デプロイの
# たびにESP32が「新しいフレームが来た」と誤認してフル書き換えを起こす。
#
# DATA_DIR がVolumeでない(=コンテナローカル)場合でも壊れはしない。
# その場合はただ復元できないだけで、ESP32は前の画像を保持したまま待つ。
# ---------------------------------------------------------------------
def _atomic_write(path, data):
    """書きかけのファイルを読まれないよう、一時ファイル経由で置き換える"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def persist(frame, preview):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write(FRAME_FILE, frame)
        if preview is not None:
            _atomic_write(PREVIEW_FILE, preview)
    except OSError as e:
        # 保存できなくても配信自体は続けられる。落とさない。
        print(f"persist failed: {e}")


def restore():
    """起動時に前回のフレームを読み直す。無ければ何もしない。"""
    global _current_frame, _current_version, _current_preview
    try:
        frame = FRAME_FILE.read_bytes()
    except OSError:
        print(f"no stored frame at {FRAME_FILE}")
        return
    if len(frame) != FRAME_BYTES:
        print(f"stored frame has wrong size ({len(frame)} != {FRAME_BYTES}), ignored")
        return
    _current_frame = frame
    _current_version = hashlib.sha1(frame).hexdigest()[:16]
    try:
        _current_preview = PREVIEW_FILE.read_bytes()
    except OSError:
        _current_preview = None
    print(f"restored frame from {FRAME_FILE}, version={_current_version}")


# ---------------------------------------------------------------------
# 絵文字の切り出し
# ---------------------------------------------------------------------
# ZWJシーケンスや肌色修飾子までは厳密に扱っていない。
# 基本的な単体絵文字を拾うだけの割り切った実装。
EMOJI_PATTERN = re.compile(
    "([\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U00002B00-\U00002BFF"
    "\U0001F1E6-\U0001F1FF"
    "\U0000FE0F\U0000200D]+)"
)


def split_emoji(text):
    """文字列を (is_emoji, chunk) のリストに分解する"""
    parts = []
    for chunk in EMOJI_PATTERN.split(text):
        if not chunk:
            continue
        parts.append((bool(EMOJI_PATTERN.fullmatch(chunk)), chunk))
    return parts


def render_emoji(ch, target_h):
    """絵文字1文字をカラーのRGBA画像として返す。失敗したらNone"""
    if FONT_EMOJI is None:
        return None
    try:
        f = ImageFont.truetype(FONT_EMOJI, EMOJI_NATIVE_SIZE)
        img = Image.new("RGBA", (EMOJI_NATIVE_SIZE + 20, EMOJI_NATIVE_SIZE + 20),
                        (255, 255, 255, 0))
        d = ImageDraw.Draw(img)
        d.text((10, 0), ch, font=f, embedded_color=True)
        bbox = img.getbbox()
        if bbox is None:
            return None
        img = img.crop(bbox)
        ratio = target_h / img.height
        return img.resize((max(1, int(img.width * ratio)), target_h), Image.LANCZOS)
    except Exception as e:
        print(f"emoji render failed for {ch!r}: {e}")
        return None


# ---------------------------------------------------------------------
# テキスト -> 画像
#
# 中央揃えには、描き始める前に各行の幅が確定している必要がある。
# そこで「行に分解する」フェーズと「描く」フェーズを分けている。
# ---------------------------------------------------------------------
def layout_text(text, draw, font, font_size, margin):
    """テキストを行のリストに分解する。

    戻り値は [(items, width), ...]。
    items の要素は ("text", 文字, 送り幅) か ("emoji", 画像, 送り幅)。
    """
    max_w = CANVAS_W - 2 * margin
    lines = []
    cur, cur_w = [], 0

    def flush():
        nonlocal cur, cur_w
        lines.append((cur, cur_w))
        cur, cur_w = [], 0

    for is_emoji, chunk in split_emoji(text):
        if is_emoji:
            for ch in chunk:
                if ch in "️‍":
                    continue
                em = render_emoji(ch, font_size)
                if em is None:
                    continue
                w = em.width + 2
                # curが空のまま折り返すと、1文字も置けずに行だけが増え続ける
                if cur and cur_w + w > max_w:
                    flush()
                cur.append(("emoji", em, w))
                cur_w += w
        else:
            # 日本語は単語境界が無いので1文字ずつ折り返す
            for ch in chunk:
                if ch == "\n":
                    flush()
                    continue
                w = draw.textlength(ch, font=font)
                if cur and cur_w + w > max_w:
                    flush()
                cur.append(("text", ch, w))
                cur_w += w
    if cur:
        flush()

    # 末尾の空行は縦位置を狂わせるだけなので落とす
    while lines and not lines[-1][0]:
        lines.pop()
    return lines


def render_text(text, font_size=32, margin=12, align="center"):
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    # アンチエイリアスを切る。有効だと文字の輪郭が中間調のグレーになるが、
    # 4色パレットではグレー73..158が「赤」に最も近いと判定されるため、
    # 黒い文字の縁が赤くなる。e-Paperはそもそも中間調を出せないので、
    # アンチエイリアス自体に意味がない。
    draw.fontmode = "1"
    font = ImageFont.truetype(FONT_JP, font_size)

    line_h = int(font_size * 1.45)
    lines = layout_text(text, draw, font, font_size, margin)

    # 入りきらない行は捨てる
    max_lines = max(1, (CANVAS_H - 2 * margin) // line_h)
    if len(lines) > max_lines:
        print(f"text overflow: {len(lines)} lines -> {max_lines}")
        lines = lines[:max_lines]

    # 中央揃えのときは、縦もブロックごと中央に置く
    block_h = len(lines) * line_h
    y = max(margin, (CANVAS_H - block_h) // 2) if align == "center" else margin

    for items, width in lines:
        x = (CANVAS_W - width) / 2 if align == "center" else margin
        for kind, payload, adv in items:
            if kind == "emoji":
                # pasteは整数座標のみ。textlengthがfloatを返すのでxはfloatになりうる。
                img.paste(payload, (round(x), round(y)), payload)
            else:
                draw.text((x, y), payload, font=font, fill=(0, 0, 0))
            x += adv
        y += line_h

    return img


# ---------------------------------------------------------------------
# 画像 -> キャンバスサイズにフィット(余白は白)
#
# 縦置きなので、縦長画像はほぼ全面を使う。
# 横長画像は幅240に合わせて縮み、上下に白い余白が出る。
# どちらも回転させないので、パネルを縦に持ったまま正しく見える。
# ---------------------------------------------------------------------
def fit_image(src):
    """画像をキャンバスに収める。全体が入るよう縮小し、余った辺は白で埋める。

    戻り値は (キャンバス, 実画像の占める領域)。
    領域は自動調整のヒストグラム判定に使う。白い余白まで含めて判定すると、
    横長写真では面積の6割が純白になり、中央値が255に張り付いて壊れる。
    """
    src = src.convert("RGB")
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), (255, 255, 255))
    ratio = min(CANVAS_W / src.width, CANVAS_H / src.height)
    new = src.resize((max(1, round(src.width * ratio)),
                      max(1, round(src.height * ratio))), Image.LANCZOS)
    ox = (CANVAS_W - new.width) // 2
    oy = (CANVAS_H - new.height) // 2
    canvas.paste(new, (ox, oy))
    print(f"fit: {src.width}x{src.height} -> キャンバス {CANVAS_W}x{CANVAS_H} "
          f"(画像 {new.width}x{new.height})")
    return canvas, (ox, oy, ox + new.width, oy + new.height)


# ---------------------------------------------------------------------
# 明るさ調整 (減色より前に掛ける)
#
# ディザリングは中間調を網点で表すが、実機のe-Paperは白が紙の白ほど
# 明るくなく、黒のドットも滲んで太るので、元画像より暗く沈んで見える。
# 減色してしまうと階調が2〜4段しか残らず後から持ち上げられないため、
# ここで先に中間調を白側へ寄せておく。
#
# brightness=100 が素通し。上げるほど明るい。ガンマなので黒潰れを
# 起こさず、中間調だけが動く。
# white_point はそれとは別に、明るい側を純白へ切り上げるためのしきい値。
# ---------------------------------------------------------------------
def auto_tone_params(img, target=185, clip=0.02):
    """画像のヒストグラムから brightness と white_point を決める。

    white_point : 明るい側から clip(既定2%) を捨てた位置。空や光沢など
                  一番明るい部分を確実に純白へ寄せる。
    brightness  : 白点で正規化した「中央値」を target に合わせるガンマから
                  逆算する。平均ではなく中央値を使うのは、広い空や大きな
                  黒つぶれのような偏った画素に引きずられないため。
    """
    hist = img.convert("L").histogram()
    total = sum(hist)
    if total == 0:
        return 100, 255

    acc = 0
    wp = 255
    for v in range(255, -1, -1):
        acc += hist[v]
        if acc >= total * clip:
            wp = v
            break
    wp = max(96, min(255, wp))

    acc = 0
    med = 128
    for v in range(256):
        acc += hist[v]
        if acc > total * 0.5:
            med = v
            break

    # 白点で割った中央値を target/255 に写すガンマ。log(0)とlog(1)を避ける。
    m = min(0.995, max(0.005, med / wp))
    gamma = math.log(target / 255.0) / math.log(m)
    gamma = max(0.25, min(4.0, gamma))

    # 自動では暗くする方向に振らない(下限100 = 素通し)。
    # e-Paperは実機が元々暗く出るので暗く寄せて嬉しいことがなく、
    # 明るい写真では白とばしが既に背景を白へ寄せているため、
    # そこへ暗いガンマを重ねると被写体の中間調だけが潰れる。
    brightness = max(100, min(400, int(round(100.0 / gamma))))
    return brightness, wp


def adjust_tone(img, brightness=100, white_point=255):
    """brightness: 100が素通し。上げるほど中間調が明るい。
    white_point : この値以上を純白に飛ばす。255で無効。

    白飛ばしが効くのは、ディザリングが明るい領域にも黒ドットを撒くため。
    元が250のような「ほぼ白」でも数%の黒が混ざり、面で見ると白が濁る。
    しきい値以上を先に255へ寄せておくと、背景が完全な白ドットだけになる。
    """
    brightness  = max(10, min(400, int(brightness)))
    white_point = max(64, min(255, int(white_point)))
    if brightness == 100 and white_point == 255:
        return img

    gamma = 100.0 / brightness          # >100 で gamma<1 になり明るくなる
    lut = [255 if i >= white_point
           else min(255, round(255.0 * (i / white_point) ** gamma))
           for i in range(256)]
    return img.point(lut * len(img.getbands()))


# ---------------------------------------------------------------------
# 減色
#   dither=True  : 写真向け。Floyd-Steinberg
#   dither=False : 文字向け。境界をぼかさない
#   bw=True      : 白黒2値。黄・赤を使わない
# ---------------------------------------------------------------------
def quantize(img, dither, bw=False):
    palette = PALETTE_BW if bw else PALETTE

    if bw:
        # 先にグレースケール化する。RGBのまま白黒2色へ最近傍を取ると、
        # 距離が輝度ではなくRGB空間上の距離になり、中間色の落ち方が
        # 人の見た目とずれる。convert("L")は輝度で潰すので素直に出る。
        img = img.convert("L").convert("RGB")

    pal_img = Image.new("P", (1, 1))
    flat = []
    for rgb, _ in palette:
        flat += list(rgb)
    flat += [0, 0, 0] * (256 - len(palette))
    pal_img.putpalette(flat)

    mode = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    q = img.quantize(palette=pal_img, dither=mode)
    return q.convert("RGB")


# ---------------------------------------------------------------------
# パッキング: 2bit/pixel、1バイトに左から4ピクセル(上位ビットが左)
# ---------------------------------------------------------------------
def pack(rgb_img):
    if rgb_img.size != (CANVAS_W, CANVAS_H):
        raise ValueError(
            f"expected canvas {(CANVAS_W, CANVAS_H)}, got {rgb_img.size}")

    # ROTATE=0 のときは回さない。rotate(0)でも動くが、無駄なコピーを避ける。
    native = rgb_img.rotate(ROTATE, expand=True) if ROTATE else rgb_img
    assert native.size == (PANEL_W, PANEL_H), \
        f"expected {(PANEL_W, PANEL_H)}, got {native.size}"

    stride = PANEL_W // 4
    out = bytearray(stride * PANEL_H)
    px = native.load()

    for y in range(PANEL_H):
        base = y * stride
        for x in range(PANEL_W):
            code = RGB2CODE.get(px[x, y], CODE_WHITE)
            out[base + (x >> 2)] |= code << (6 - 2 * (x & 3))

    return bytes(out)


def publish(rgb_img):
    global _current_frame, _current_version, _current_preview
    frame = pack(rgb_img)
    version = hashlib.sha1(frame).hexdigest()[:16]

    buf = io.BytesIO()
    rgb_img.save(buf, format="PNG")

    preview = buf.getvalue()

    # 重い処理は全てロックの外で終わらせ、差し替えだけを不可分に行う。
    with _lock:
        _current_frame = frame
        _current_version = version
        _current_preview = preview

    # ディスクI/Oもロックの外。待っている /wait を止めない。
    persist(frame, preview)
    print(f"published: {len(frame)} bytes, version={version}")


# ---------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------
PAGE = """
<!doctype html>
<html lang="ja">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>e-Paper</title>
<style>
 :root{
   --bg:#f4f4f5; --card:#fff; --fg:#18181b; --muted:#71717a;
   --line:#e4e4e7; --accent:#2563eb; --accent-fg:#fff; --field:#fff;
   --shadow:0 1px 2px rgba(0,0,0,.06),0 1px 8px rgba(0,0,0,.04);
 }
 @media (prefers-color-scheme:dark){
   :root{
     --bg:#18181b; --card:#232327; --fg:#f4f4f5; --muted:#a1a1aa;
     --line:#3f3f46; --accent:#3b82f6; --accent-fg:#fff; --field:#1c1c20;
     --shadow:0 1px 2px rgba(0,0,0,.4);
   }
 }
 *{box-sizing:border-box}
 body{
   margin:0; padding:24px 16px 64px; background:var(--bg); color:var(--fg);
   font-family:system-ui,-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif;
   line-height:1.6; -webkit-text-size-adjust:100%;
 }
 .wrap{max-width:760px;margin:0 auto}
 header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:20px}
 h1{font-size:20px;margin:0;letter-spacing:.02em}
 .ver{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
      color:var(--muted);background:var(--card);border:1px solid var(--line);
      padding:2px 8px;border-radius:999px}
 .grid{display:grid;grid-template-columns:1fr;gap:16px}
 @media (min-width:720px){ .grid{grid-template-columns:1fr 260px} }
 .card{background:var(--card);border:1px solid var(--line);border-radius:14px;
       padding:18px;box-shadow:var(--shadow)}
 .card h2{font-size:14px;margin:0 0 14px;color:var(--muted);
          font-weight:600;letter-spacing:.06em}
 label{font-size:13px;color:var(--muted)}
 .row{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:10px 0}
 .row>label{display:inline-flex;align-items:center;gap:6px;color:var(--fg);font-size:13px}
 textarea,input[type=number],select,input[type=file]{
   font:inherit;font-size:14px;color:var(--fg);background:var(--field);
   border:1px solid var(--line);border-radius:9px;padding:8px 10px;
 }
 textarea{width:100%;height:104px;resize:vertical}
 input[type=number]{width:82px}
 button{font:inherit;font-size:14px;font-weight:600;border-radius:9px;
        padding:9px 18px;border:1px solid transparent;cursor:pointer}
 .primary{background:var(--accent);color:var(--accent-fg)}
 .primary:hover{filter:brightness(1.08)}
 .ghost{background:transparent;color:var(--fg);border-color:var(--line)}
 .ghost:hover{background:var(--bg)}
 .ghost[aria-pressed=true]{background:var(--accent);color:var(--accent-fg);
                           border-color:var(--accent)}
 small{color:var(--muted);font-size:12px}
 .preview{position:sticky;top:24px;text-align:center}
 .screen{display:inline-block;background:#fff;border:6px solid #3f3f46;
         border-radius:10px;line-height:0;max-width:100%}
 .screen img{width:200px;max-width:100%;height:auto;image-rendering:pixelated}
 .empty{color:var(--muted);font-size:13px;padding:40px 0}
 /* touch-action:none で指のスクロール、user-select:none でドラッグ選択を止める。
    pointerdown の preventDefault だけでは選択は止まらない。Pointer Events の
    仕様上、pointerdown を打ち消しても互換の mousedown は発生し、
    そこから文字選択が始まってしまうため。 */
 #pad{width:100%;max-width:240px;aspect-ratio:240/416;touch-action:none;
      background:#fff;border:1px solid var(--line);border-radius:10px;
      display:block;margin:0 auto;cursor:crosshair;
      -webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
 /* キャンバスの外へドラッグが抜けたときに、周りの文字が選択されるのを防ぐ。
    描いている間だけ付ける。 */
 body.drawing,body.drawing *{-webkit-user-select:none;user-select:none}
 .pad-tools{display:flex;gap:6px;justify-content:center;margin:10px 0;flex-wrap:wrap}
 .pad-tools button{padding:7px 12px}
 .pad-tools .sep{width:1px;background:var(--line);margin:2px 4px}
 button:disabled{opacity:.35;cursor:default}
 button:disabled:hover{background:transparent}
</style>
<body>
<div class="wrap">
 <header>
  <h1>e-Paper</h1>
  <span class="ver">{{ version }}</span>
 </header>

 <div class="grid">
  <div style="display:grid;gap:16px">

   <section class="card">
    <h2>テキスト</h2>
    <form method="post" action="/text">
     <textarea name="text" placeholder="こんにちは 🔥"></textarea>
     <div class="row">
      <label>文字サイズ <input type="number" name="size" value="28" min="12" max="80"></label>
      <label>配置
       <select name="align">
        <option value="center" selected>中央</option>
        <option value="left">左</option>
       </select></label>
     </div>
     <button class="primary">送信</button>
    </form>
   </section>

   <section class="card">
    <h2>手書き</h2>
    <canvas id="pad" width="480" height="832"></canvas>
    <div class="pad-tools">
     <button type="button" class="ghost" id="pen" aria-pressed="true">ペン</button>
     <button type="button" class="ghost w" data-w="4" aria-pressed="false">細</button>
     <button type="button" class="ghost w" data-w="6" aria-pressed="false">中</button>
     <button type="button" class="ghost w" data-w="9" aria-pressed="true">太</button>
     <span class="sep"></span>
     <button type="button" class="ghost" id="eraser" aria-pressed="false">消しゴム</button>
    </div>
    <div class="pad-tools">
     <button type="button" class="ghost" id="undo" disabled>戻る</button>
     <button type="button" class="ghost" id="redo" disabled>進む</button>
     <button type="button" class="ghost" id="clear">全消去</button>
    </div>
    <div style="text-align:center">
     <button type="button" class="primary" id="send">送信</button>
     <div><small id="pad-msg"></small></div>
    </div>
   </section>

   <section class="card">
    <h2>画像</h2>
    <form method="post" action="/image" enctype="multipart/form-data">
     <div class="row"><input type="file" name="file" accept="image/*"></div>
     <div class="row">
      <label>色
       <select name="color">
        <option value="bw" selected>白黒</option>
        <option value="color">4色 (黒白黄赤)</option>
       </select></label>
      <label><input type="checkbox" name="dither" checked> ディザリング</label>
     </div>
     <div class="row">
      <label><input type="checkbox" name="auto" checked> 明るさを自動調整</label>
     </div>
     <div class="row">
      <label>明るさ <input type="number" name="brightness" value="140" min="10" max="400" step="1"></label>
      <label>白とばし <input type="number" name="white_point" value="235" min="64" max="255" step="1"></label>
     </div>
     <small>自動調整をオンにすると、上の2つは画像ごとに計算した値で上書きされます。</small>
     <div class="row"><button class="primary">送信</button></div>
    </form>
   </section>

  </div>

  <aside class="card preview">
   <h2>プレビュー</h2>
   {% if has_preview %}
   <div class="screen"><img src="/preview.png?v={{ version }}" alt="現在の表示"></div>
   {% else %}
   <div class="empty">まだ何も送信されていません</div>
   {% endif %}
  </aside>
 </div>
</div>

<script>
(function(){
 var pad = document.getElementById('pad');
 var ctx = pad.getContext('2d');
 var ERASER_W = 34;
 var mode = 'pen', penW = 9, drawing = false;

 // 履歴はビットマップではなく「線そのもの」で持つ。
 // 480x832のImageDataは1枚あたり約1.6MBあり、数十手ぶん抱えるとスマホで
 // 苦しい。線の配列なら数KBで済み、やり直しは描き直すだけでよい。
 var strokes = [], redoStack = [], current = null;

 function paint(s){
   if(s.mode === 'clear'){
     ctx.fillStyle = '#fff';
     ctx.fillRect(0, 0, pad.width, pad.height);
     return;
   }
   ctx.lineCap = 'round';
   ctx.lineJoin = 'round';
   ctx.strokeStyle = (s.mode === 'pen') ? '#000' : '#fff';
   ctx.lineWidth = s.w;
   ctx.beginPath();
   ctx.moveTo(s.pts[0][0], s.pts[0][1]);
   // 1点だけの筆跡(タップ)は、同じ座標へlineToすると丸キャップで点が出る
   if(s.pts.length === 1) ctx.lineTo(s.pts[0][0], s.pts[0][1]);
   else for(var i = 1; i < s.pts.length; i++) ctx.lineTo(s.pts[i][0], s.pts[i][1]);
   ctx.stroke();
 }
 function redraw(){
   ctx.fillStyle = '#fff';
   ctx.fillRect(0, 0, pad.width, pad.height);
   for(var i = 0; i < strokes.length; i++) paint(strokes[i]);
 }
 function updateButtons(){
   document.getElementById('undo').disabled = (strokes.length === 0);
   document.getElementById('redo').disabled = (redoStack.length === 0);
 }
 redraw();
 updateButtons();

 function setMode(m){
   mode = m;
   document.getElementById('pen').setAttribute('aria-pressed', m === 'pen');
   document.getElementById('eraser').setAttribute('aria-pressed', m === 'eraser');
 }
 function setWidth(w){
   penW = w;
   var bs = document.querySelectorAll('.pad-tools .w');
   for(var i = 0; i < bs.length; i++){
     bs[i].setAttribute('aria-pressed', Number(bs[i].dataset.w) === w);
   }
   setMode('pen');          // 太さを選んだらペンに戻すのが自然
 }
 document.getElementById('pen').onclick    = function(){ setMode('pen'); };
 document.getElementById('eraser').onclick = function(){ setMode('eraser'); };
 (function(){
   var bs = document.querySelectorAll('.pad-tools .w');
   for(var i = 0; i < bs.length; i++){
     (function(b){ b.onclick = function(){ setWidth(Number(b.dataset.w)); }; })(bs[i]);
   }
 })();

 function undo(){
   if(!strokes.length) return;
   redoStack.push(strokes.pop());
   redraw(); updateButtons();
 }
 function redo(){
   if(!redoStack.length) return;
   strokes.push(redoStack.pop());
   redraw(); updateButtons();
 }
 function clearPad(){
   // 全消去も履歴に積む。戻るで取り消せる方が事故が怖くない。
   strokes.push({mode:'clear'});
   redoStack.length = 0;
   redraw(); updateButtons();
 }
 document.getElementById('undo').onclick  = undo;
 document.getElementById('redo').onclick  = redo;
 document.getElementById('clear').onclick = clearPad;

 document.addEventListener('keydown', function(e){
   if(!(e.metaKey || e.ctrlKey) || e.key.toLowerCase() !== 'z') return;
   e.preventDefault();
   if(e.shiftKey) redo(); else undo();
 });

 // 表示サイズとキャンバスの内部解像度は違うので、座標を換算する
 function pos(e){
   var r = pad.getBoundingClientRect();
   return [(e.clientX - r.left) * pad.width / r.width,
           (e.clientY - r.top)  * pad.height / r.height];
 }
 function beginStroke(p){
   current = {mode: mode, w: (mode === 'pen') ? penW : ERASER_W, pts: [p]};
   ctx.lineCap = 'round';
   ctx.lineJoin = 'round';
   ctx.strokeStyle = (mode === 'pen') ? '#000' : '#fff';
   ctx.lineWidth = current.w;
   ctx.beginPath();
   ctx.moveTo(p[0], p[1]);
   ctx.lineTo(p[0], p[1]);
   ctx.stroke();
   ctx.beginPath();
   ctx.moveTo(p[0], p[1]);
 }
 function extendStroke(p){
   current.pts.push(p);
   ctx.lineTo(p[0], p[1]);
   ctx.stroke();
   ctx.beginPath();
   ctx.moveTo(p[0], p[1]);
 }
 function endStroke(){
   drawing = false;
   document.body.classList.remove('drawing');
   // pointerup はパネルとwindowの両方から飛んでくるので、二重に積まない
   if(current){
     strokes.push(current);
     redoStack.length = 0;   // 新しく描いたら「進む」は無効になる
     current = null;
     updateButtons();
   }
 }
 pad.addEventListener('pointerdown', function(e){
   drawing = true;
   document.body.classList.add('drawing');
   pad.setPointerCapture(e.pointerId);
   beginStroke(pos(e));
   e.preventDefault();
 });
 pad.addEventListener('pointermove', function(e){
   if(drawing) extendStroke(pos(e));
   e.preventDefault();
 });
 // pointerleave は入れない。setPointerCapture しているので枠の外へ出ても
 // 描き続けられるのに、これがあると縁をかすめただけで線が途切れる。
 ['pointerup','pointercancel'].forEach(function(ev){
   pad.addEventListener(ev, endStroke);
 });
 // 取りこぼし対策。キャンバス外でボタンを離したときもここで確実に終わる。
 window.addEventListener('pointerup', endStroke);
 // 選択とドラッグ&ドロップの既定動作を明示的に殺す
 pad.addEventListener('selectstart', function(e){ e.preventDefault(); });
 pad.addEventListener('dragstart',   function(e){ e.preventDefault(); });
 pad.addEventListener('contextmenu', function(e){ e.preventDefault(); });

 var msg = document.getElementById('pad-msg');
 document.getElementById('send').onclick = function(){
   msg.textContent = '送信中...';
   pad.toBlob(function(blob){
     var fd = new FormData();
     fd.append('file', blob, 'draw.png');
     fetch('/draw', {method:'POST', body:fd}).then(function(res){
       if(res.ok){ location.reload(); }
       else { msg.textContent = '送信に失敗しました (' + res.status + ')'; }
     }).catch(function(err){ msg.textContent = '送信に失敗しました: ' + err; });
   }, 'image/png');
 };
})();
</script>
"""


@app.route("/")
def index():
    return render_template_string(PAGE,
                                  version=_current_version,
                                  has_preview=_current_preview is not None)


@app.route("/text", methods=["POST"])
def post_text():
    text = request.form.get("text", "")
    size = int(request.form.get("size", 32))
    align = request.form.get("align", "center")
    img = render_text(text, font_size=size, align=align)
    publish(quantize(img, dither=False))     # 文字はディザリングしない
    return index()


@app.route("/image", methods=["POST"])
def post_image():
    f = request.files.get("file")
    if not f:
        return "no file", 400
    dither = request.form.get("dither") is not None
    bw = request.form.get("color", "bw") == "bw"
    brightness = int(request.form.get("brightness", 140))
    white_point = int(request.form.get("white_point", 235))
    src = Image.open(f.stream)
    img, box = fit_image(src)
    if request.form.get("auto") is not None:
        # 判定は実画像の領域だけで行う。containの白い余白まで含めると、
        # 横長写真では面積の6割が純白になり、中央値が255に張り付いて
        # どんな写真でも判定が成立しなくなる。
        brightness, white_point = auto_tone_params(img.crop(box))
        print(f"auto tone: brightness={brightness} white_point={white_point}")
    img = adjust_tone(img, brightness, white_point)
    publish(quantize(img, dither=dither, bw=bw))
    return index()


@app.route("/draw", methods=["POST"])
def post_draw():
    """手書きキャンバスのPNGを受け取る。

    キャンバスは 480x832 (パネルの2倍) で描かれる。等倍で描かせると
    線がジャギーになるので、2倍で描いてここで縮小する。
    """
    f = request.files.get("file")
    if not f:
        return "no file", 400

    img = Image.open(f.stream)
    # canvasのtoBlobはアルファ付きで来る。透明部分は白地として扱う。
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        img = Image.alpha_composite(Image.new("RGBA", img.size, (255,) * 4), img)
    img = img.convert("RGB")

    if img.size != (CANVAS_W, CANVAS_H):
        img = img.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)

    # 手書きは黒一色。ディザリングすると線が網点に割れるので掛けない。
    publish(quantize(img, dither=False, bw=True))
    return "", 204


@app.route("/wait")
def wait():
    """ロングポーリング。
    since と現在のversionが違えば即返す。
    同じなら最大 POLL_HOLD_S 秒待って204。
    ESP32側のタイムアウト(POLL_TIMEOUT_MS)より短くしておくこと。
    """
    since = request.args.get("since", "")
    deadline = time.time() + POLL_HOLD_S
    while time.time() < deadline:
        frame, version = snapshot()
        if frame is not None and version != since:
            return Response(version, mimetype="text/plain")
        time.sleep(0.3)
    return Response(status=204)


@app.route("/healthz")
def healthz():
    """Railwayのヘルスチェック用。認証不要 (PUBLIC_PATHS)。

    フレームがまだ無くても200を返す。ここで503を返すと、
    一度も投稿していない状態でデプロイが失敗扱いになる。
    """
    frame, version = snapshot()
    return Response(f"ok version={version} frame={'yes' if frame else 'no'}\n",
                    mimetype="text/plain")


@app.route("/version")
def version():
    return Response(_current_version, mimetype="text/plain")


@app.route("/frame.bin")
def frame():
    data, version = snapshot()
    if data is None:
        return "no frame", 404
    # 中身に対応するversionを一緒に返す。ESP32はこれを記録することで、
    # ダウンロードした内容と記録したversionが食い違わなくなる。
    resp = Response(data, mimetype="application/octet-stream")
    resp.headers["X-Frame-Version"] = version
    return resp


@app.route("/preview.png")
def preview():
    if _current_preview is None:
        return "no preview", 404
    return send_file(io.BytesIO(_current_preview), mimetype="image/png")


def lan_ip():
    """このPCがLANで使っているIPを調べる。外部に通信はしない"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 1))     # TEST-NET-1。到達しなくてよい
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# 保存済みフレームの復元は、importされた時点で行う。
# gunicorn経由(Railway)では __main__ ブロックが実行されないため、
# ここに置かないと本番で復元されない。
restore()

if not EPD_PASS:
    print("WARNING: EPD_PASS が未設定です。認証なしで公開されます。")

if __name__ == "__main__":
    ip = lan_ip()
    print("=" * 60)
    print(f"  ブラウザ      : http://{ip}:{PORT}/")
    print(f"  日本語フォント: {FONT_JP}")
    print(f"  絵文字フォント: {FONT_EMOJI or '(なし。絵文字は描画されません)'}")
    print(f"  データ保存先  : {DATA_DIR}")
    print(f"  ESP32.ino の SERVER_BASE をこれに合わせること:")
    print(f"      static const char* SERVER_BASE = \"http://{ip}:{PORT}\";")
    print("=" * 60)
    # threaded=True は必須。/wait がブロックしている間も
    # ブラウザからの投稿を受け付けられるようにする。
    # 本番(Railway)はこの経路を通らず、gunicornが server:app を読み込む。
    app.run(host="0.0.0.0", port=PORT, threaded=True)