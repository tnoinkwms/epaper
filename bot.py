#!/usr/bin/env python3
"""
bot.py - Discordに投稿された画像をe-Paperへ流す中継Bot

  Railwayに「もう1つのサービス」として常駐させる。
  同じリポジトリ・同じDockerfileで、起動コマンドだけ `python bot.py` にする。

  画像処理(減色・明るさ自動調整・パッキング)は一切やらない。
  添付をダウンロードして server.py の /image へPOSTするだけの中継役。
  表示仕様を変えたいときは server.py 側だけ直せばよい。

環境変数:
  DISCORD_TOKEN       Botトークン。絶対にソースに書かないこと
  EPD_URL             e-Paperサーバのベース URL (末尾スラッシュなし)
  EPD_USER            Basic認証のユーザ名 (既定 epd)
  EPD_PASS            Basic認証のパスワード。空なら認証ヘッダを送らない
  ALLOWED_USER_IDS    使用を許可するDiscordユーザーID。カンマ区切り。★必須
  ALLOWED_CHANNEL_IDS 反応するチャンネルID。カンマ区切り。空なら全チャンネル
  POST_TEXT           1 ならテキスト投稿も /text に流す (既定 0)
"""

import io
import os
import sys
import logging

import aiohttp
import discord

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("epd-bot")


def _ids(name):
    """カンマ区切りの環境変数を int の集合にする"""
    raw = os.environ.get(name, "")
    out = set()
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            log.warning("%s に数値でない値がある: %r (無視)", name, part)
    return out


TOKEN        = os.environ.get("DISCORD_TOKEN", "")
EPD_URL      = os.environ.get("EPD_URL", "").rstrip("/")
EPD_USER     = os.environ.get("EPD_USER", "epd")
EPD_PASS     = os.environ.get("EPD_PASS", "")
ALLOWED_USERS    = _ids("ALLOWED_USER_IDS")
ALLOWED_CHANNELS = _ids("ALLOWED_CHANNEL_IDS")
POST_TEXT    = os.environ.get("POST_TEXT", "0") == "1"

# Discordの添付上限に合わせた保険。サーバ側で縮小するので大きい必要はない。
MAX_BYTES = 20 * 1024 * 1024


def preflight():
    """設定の不備は起動時に落とす。黙って誤動作させない。"""
    bad = []
    if not TOKEN:
        bad.append("DISCORD_TOKEN が未設定")
    if not EPD_URL:
        bad.append("EPD_URL が未設定")
    # 許可リストが空のときに「全員許可」になると、Botがいるサーバーの
    # 誰でも表示を書き換えられてしまう。空なら起動させない(fail-closed)。
    if not ALLOWED_USERS:
        bad.append("ALLOWED_USER_IDS が空。誰も許可されない設定で起動はしない")
    if bad:
        for b in bad:
            log.error("設定エラー: %s", b)
        sys.exit(1)


intents = discord.Intents.default()
intents.message_content = True          # 添付と本文を読むのに必要
client = discord.Client(intents=intents)

_session: aiohttp.ClientSession | None = None


def auth():
    return aiohttp.BasicAuth(EPD_USER, EPD_PASS) if EPD_PASS else None


async def push_image(data: bytes, filename: str) -> tuple[bool, str]:
    """/image へ中継する。戻り値は (成功したか, 表示用メッセージ)"""
    form = aiohttp.FormData()
    form.add_field("file", io.BytesIO(data), filename=filename or "upload.png")
    # Web UI の既定値に合わせる。ここを変えるより server.py 側を直す方がよい。
    form.add_field("color", "bw")
    form.add_field("dither", "on")
    form.add_field("auto", "on")
    try:
        async with _session.post(f"{EPD_URL}/image", data=form,
                                 auth=auth(), timeout=aiohttp.ClientTimeout(total=60)) as r:
            body = (await r.text())[:200]
            if r.status in (200, 204):
                return True, ""
            if r.status == 401:
                return False, "サーバの認証に失敗しました (EPD_USER/EPD_PASS を確認)"
            return False, f"サーバが {r.status} を返しました: {body}"
    except Exception as e:
        return False, f"サーバに届きませんでした: {e}"


async def push_text(text: str) -> tuple[bool, str]:
    data = {"text": text, "size": "28", "align": "center"}
    try:
        async with _session.post(f"{EPD_URL}/text", data=data,
                                 auth=auth(), timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status in (200, 204):
                return True, ""
            return False, f"サーバが {r.status} を返しました"
    except Exception as e:
        return False, f"サーバに届きませんでした: {e}"


async def selftest():
    """起動時に中継先へ疎通と認証を確認してログに残す。

    これが無いと、EPD_URL や EPD_PASS を間違えていても
    「誰かが画像を投稿するまで気づかない」ことになる。
    """
    t = aiohttp.ClientTimeout(total=20)
    try:
        async with _session.get(f"{EPD_URL}/healthz", timeout=t) as r:
            body = (await r.text()).strip()
            log.info("疎通OK  /healthz -> %s %s", r.status, body)
    except Exception as e:
        log.error("中継先に到達できない: %s", e)
        log.error("  EPD_URL を確認。https:// から始まり、末尾スラッシュ無し: %r", EPD_URL)
        return
    try:
        async with _session.get(f"{EPD_URL}/version", auth=auth(), timeout=t) as r:
            if r.status == 401:
                log.error("認証NG /version -> 401")
                log.error("  EPD_USER/EPD_PASS がサーバ側の設定と一致していない")
            else:
                log.info("認証OK  /version -> %s %s", r.status, (await r.text()).strip())
    except Exception as e:
        log.error("認証確認に失敗: %s", e)


@client.event
async def on_ready():
    global _session
    if _session is None:
        _session = aiohttp.ClientSession()
    log.info("ログイン: %s", client.user)
    log.info("許可ユーザー: %s", sorted(ALLOWED_USERS))
    log.info("対象チャンネル: %s", sorted(ALLOWED_CHANNELS) or "(全部)")
    log.info("中継先: %s (認証=%s)", EPD_URL, bool(EPD_PASS))
    log.info("テキスト投稿の中継: %s", "有効" if POST_TEXT else "無効 (POST_TEXT=1 で有効)")
    await selftest()


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    images = [a for a in message.attachments
              if (a.content_type or "").startswith("image/")]
    has_text = bool(message.content.strip())

    # 画像もテキストも無い発言はそもそも対象外。ここで黙って帰ってよい。
    if not message.attachments and not has_text:
        return

    # 添付はあるが画像ではなかった場合は、理由を残す。
    if message.attachments and not images:
        kinds = [a.content_type for a in message.attachments]
        log.info("無視: 画像でない添付 %s (channel=%s)", kinds, message.channel.id)
        return

    # チャンネル判定は「中継する価値のある投稿」だと分かってから行う。
    # 先にやると、関係ない雑談まで全部ログに出てしまう。
    if ALLOWED_CHANNELS and message.channel.id not in ALLOWED_CHANNELS:
        log.info("無視: 対象外チャンネル channel_id=%s (#%s) / 許可=%s",
                 message.channel.id, getattr(message.channel, "name", "?"),
                 sorted(ALLOWED_CHANNELS))
        log.info("  このチャンネルで使うなら ALLOWED_CHANNEL_IDS に %s を足す",
                 message.channel.id)
        return

    if not images and not POST_TEXT:
        log.info("無視: テキストのみ (POST_TEXT=1 にすると /text へ中継する)")
        return

    # ---- 許可チェック ----
    # チャンネルを限定していても、そのチャンネルを見られる人は全員投稿できる。
    # 実際の権限はここで決める。
    if message.author.id not in ALLOWED_USERS:
        log.info("拒否: %s (id=%s) は ALLOWED_USER_IDS に無い / 許可=%s",
                 message.author, message.author.id, sorted(ALLOWED_USERS))
        try:
            await message.add_reaction("🚫")
        except discord.HTTPException:
            pass
        return

    if images:
        a = images[0]
        if a.size > MAX_BYTES:
            await message.reply(f"画像が大きすぎます ({a.size/1024/1024:.1f}MB)")
            return
        if len(images) > 1:
            await message.reply(f"画像が{len(images)}枚ありました。1枚目だけ表示します。")
        try:
            data = await a.read()
        except discord.HTTPException as e:
            await message.reply(f"Discordから画像を取得できませんでした: {e}")
            return
        ok, err = await push_image(data, a.filename)
    else:
        ok, err = await push_text(message.content.strip())

    try:
        await message.add_reaction("✅" if ok else "⚠️")
    except discord.HTTPException:
        pass
    if not ok:
        await message.reply(err)
    else:
        log.info("反映: %s が投稿", message.author)


def main():
    preflight()
    client.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
