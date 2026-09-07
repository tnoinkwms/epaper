/*
 * ESP32.ino - e-Paper 3.7inch (G) 常時給電・長期運用版
 *              サーバはRailwayに常設 (HTTPS + Basic認証)
 *
 * 設計方針:
 *   - millis()のオーバーフロー(49.7日)に耐える
 *   - WiFi断・サーバ断から自力で復帰する
 *   - どこかで固まってもウォッチドッグで復帰する
 *   - 定期的に計画再起動して内部状態をリセットする
 *   - パネルの電源は更新時以外は必ず落とす(寿命保護)
 *   - 更新間隔の下限をファームウェア側で強制する
 *
 * e-Paperは表示保持型なので、再起動しても画面は消えない。
 * したがって再起動は利用者から見て不可視であり、安全策として使える。
 *
 * LAN内のPCではなくRailway上のサーバを見るので、
 *   - 接続先はグローバルなHTTPS。ルータのIP固定やポート開放は不要
 *   - サーバ側が再デプロイで一時的に落ちても、指数バックオフで復帰する
 *   - 認証はBasic。EPD_USER / EPD_PASS をRailwayの環境変数と一致させること
 *
 * 必要なライブラリ: WiFiManager (tzapu)
 */

#include <WiFi.h>
#include <WiFiManager.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include "esp_task_wdt.h"
#include "qrcode.h"

#include "DEV_Config.h"
#include "EPD_3in7g.h"
#include "GUI_Paint.h"
#include "fonts.h"

// ==================== 設定 ====================
// Railwayが発行した公開ドメイン。末尾にスラッシュを付けないこと。
// Railwayのダッシュボード > Settings > Networking > Public Networking に出る。
//
// 必ず https:// で書く。RailwayはHTTPを301でHTTPSへ飛ばすが、HTTPClientは
// 既定でリダイレクトを追わないので、http:// のままだと301を受け取って終わる。
// (http:// で書けばTLS無しでも動く実装にはしてあるので、LAN内のPCで
//  server.py を直接動かしてデバッグしたいときはそのURLを入れればよい)
static const char* SERVER_BASE = "https://CHANGE-ME.up.railway.app";

// Basic認証。Railwayの環境変数 EPD_USER / EPD_PASS と完全に一致させること。
// DEVICE_PASS を空文字にすると認証ヘッダを送らない(サーバ側も認証なしのとき用)。
static const char* DEVICE_USER = "epd";
static const char* DEVICE_PASS = "CHANGE-ME";

static const char* AP_SSID     = "EPD-Setup";
static const char* QR_PAYLOAD  = "WIFI:T:nopass;S:EPD-Setup;;";

// サーバの /wait は既定25秒で204を返す(環境変数 POLL_HOLD_S)。
// TLSハンドシェイクとインターネット越しの遅延を足しても届く長さにしておく。
// サーバ側を伸ばすときは、必ずこちらを先に伸ばすこと。
static const uint32_t POLL_TIMEOUT_MS      = 40000;                  // ロングポーリング待受
static const uint32_t CONNECT_TIMEOUT_MS   = 10000;                  // TLS込みの接続確立
static const uint32_t MIN_REFRESH_GAP_MS   = 180UL * 1000;           // メーカー推奨の下限
static const uint64_t FORCED_REFRESH_MS    = 23ULL * 3600 * 1000;    // 焼付防止の強制更新
// n回ごとに完全初期化(フル波形)する。1なら毎回。
// Init_Fast は波形を端折るぶん粒子が動ききらず、白が濁って残像も溜まる。
// 更新は最低でも3分に1回なので、速度を捨てて毎回フル波形にした方が白が出る。
// 書き換えの点滅が気になるなら 5〜20 に戻す。
static const uint32_t DEGHOST_EVERY        = 1;
static const uint64_t PLANNED_REBOOT_MS    = 7ULL * 24 * 3600 * 1000;// 計画再起動(7日)
static const uint32_t WDT_TIMEOUT_S        = 90;                     // 描画20秒に対し十分な余裕
// TLSは接続のたびにmbedtlsのバッファを40KB前後確保して解放する。
// 確保できない状態まで痩せたら、粘らず再起動した方が復帰が速い。
// (平文HTTPだけだった頃の40000では、確保に失敗し続けても再起動しなかった)
static const uint32_t HEAP_FLOOR           = 60000;                  // 空きヒープの下限
static const uint32_t WIFI_GRACE_MS        = 60000;                  // 切断を許容する時間
static const uint32_t MAX_WIFI_RETRY       = 5;
// ==============================================

// TLSハンドシェイクはHTTPClientのString操作と合わせてスタックを深く使う。
// Arduinoのloopタスクは既定8KBで、平文HTTPなら足りるがTLSでは溢れうる。
// 溢れると Stack canary watchpoint triggered (loopTask) で落ちる。
#if defined(SET_LOOP_TASK_STACK_SIZE)
SET_LOOP_TASK_STACK_SIZE(16 * 1024);
#endif

// 電源断では消えるが、ソフトウェアリセットでは保持されるRTC領域。
// マジックナンバーで「電源投入直後かどうか」を判別する。
#define RTC_MAGIC 0xE9AD0137
RTC_DATA_ATTR uint32_t rtc_magic   = 0;
RTC_DATA_ATTR char     rtc_version[24] = "";
RTC_DATA_ATTR uint32_t rtc_reboots = 0;
// 設定画面が今パネルに出ているか。再起動を跨いで覚えておく必要がある。
RTC_DATA_ATTR uint32_t rtc_setupShown = 0;

static UBYTE*  g_image     = NULL;
static UWORD   g_imageSize = 0;

// ---- オーバーフロー安全な稼働時間 ----
static uint64_t g_uptimeMs   = 0;
static uint32_t g_lastMillis = 0;

static void tickUptime() {
  uint32_t now = millis();
  g_uptimeMs += (uint32_t)(now - g_lastMillis);   // 符号なし減算はラップしても正しい
  g_lastMillis = now;
}
static uint64_t uptime() { tickUptime(); return g_uptimeMs; }

// ---- 状態 ----
static uint64_t g_lastRefreshAt   = 0;
static uint64_t g_lastFullInitAt  = 0;
static uint32_t g_refreshCount    = 0;
static bool     g_haveRefreshed   = false;
static uint32_t g_serverFailures  = 0;
static uint64_t g_wifiLostAt      = 0;
static uint32_t g_wifiRetries     = 0;
static char     g_version[24]     = "";
// 直前の fetchFrame() が実際に受け取った中身のversion (X-Frame-Version)。
// レート制限の待機中にサーバが更新されても、描いた内容と記録する版がずれない。
static char     g_fetchedVersion[24] = "";

static void feedWdt() { esp_task_wdt_reset(); }

// ---------------------------------------------------------------
// ネットワーククライアント
//
// SERVER_BASE のスキームを見て、TLSと平文を切り替える。
// クライアントはグローバルに1個ずつ持つ。毎回スタックに作ると、
// mbedtlsのコンテキストが確保・解放を繰り返してヒープが断片化しやすい。
//
// 証明書は検証していない(setInsecure)。検証するには
//   1. NTPで時刻を合わせる(証明書の有効期限判定に必要)
//   2. ルート証明書をファームウェアに焼き込む
// の両方が要るうえ、RailwayがCAを切り替えた瞬間に端末が無言で止まる。
// ここで守りたいのは「他人に勝手な画像を出させないこと」で、それは
// サーバ側のBasic認証で担保している。盗聴されて困る中身も無い。
// ---------------------------------------------------------------
static WiFiClientSecure g_tls;
static WiFiClient       g_tcp;
static bool             g_useTls = false;

static WiFiClient& netClient() {
  return g_useTls ? (WiFiClient&)g_tls : g_tcp;
}

// HTTPClient::end() は、サーバから先に切られていると内部の stop() を
// 呼ばずに帰ることがある。TLSではそれがmbedtlsコンテキストの解放漏れに
// なるので、リクエストのたびに明示的に閉じる。stop()は多重呼び出し安全。
static void netClose() {
  netClient().stop();
}

static void applyAuth(HTTPClient& http) {
  // begin() の後、GET() の前に呼ぶこと。begin() が内部状態を作り直す。
  if (DEVICE_PASS[0] != '\0') http.setAuthorization(DEVICE_USER, DEVICE_PASS);
}

// ---------------------------------------------------------------
// バッファ (起動時に一度だけ確保し、以後解放しない = 断片化しない)
// ---------------------------------------------------------------
static bool allocBuffer() {
  if (g_image) return true;
  g_imageSize = ((EPD_3IN7G_WIDTH % 4 == 0) ? (EPD_3IN7G_WIDTH / 4)
                                            : (EPD_3IN7G_WIDTH / 4 + 1))
                * EPD_3IN7G_HEIGHT;
  g_image = (UBYTE*)malloc(g_imageSize);
  if (!g_image) {
    Serial.println("FATAL: malloc failed");
    return false;
  }
  Serial.printf("framebuffer: %u bytes\n", g_imageSize);
  return true;
}

// ---------------------------------------------------------------
// パネル電源
// ---------------------------------------------------------------
static void panelOn() {
  DEV_Module_Init();          // PWR=HIGH を含む
  delay(50);
}

static void panelOff() {
  EPD_3IN7G_Sleep();
  delay(2000);                // 仕様上、最低2秒必要
  DEV_Module_Exit();          // PWR=LOW
}

// ---------------------------------------------------------------
// QR (初回プロビジョニング時のみ使用)
// ---------------------------------------------------------------
// 縦置きレイアウト。QRを上に置き、説明文をその下に積む。
static const int QR_AREA_TOP = 25;    // 白の余白(quiet zone)込みの上端
static const int QR_AREA_MAX = 210;   // 白の余白込みで許す最大辺長

static void qrDisplayCallback(esp_qrcode_handle_t qr) {
  // モジュール数はペイロード長で変わる(v2=25, v3=29, v4=33)。
  // 固定値で組むと中央からずれるので、実測値から毎回組み立てる。
  const int size = esp_qrcode_get_size(qr);
  const int quietMods = 4;                        // 規格上の最小余白
  int scale = QR_AREA_MAX / (size + 2 * quietMods);
  if (scale < 1) scale = 1;

  const int side  = size * scale;
  const int quiet = quietMods * scale;
  const int total = side + 2 * quiet;
  const int x0 = (EPD_3IN7G_WIDTH - total) / 2 + quiet;   // QR本体の左上
  const int y0 = QR_AREA_TOP + quiet;

  Paint_DrawRectangle(x0 - quiet, y0 - quiet,
                      x0 + side + quiet, y0 + side + quiet,
                      EPD_3IN7G_WHITE, DOT_PIXEL_1X1, DRAW_FILL_FULL);
  for (int y = 0; y < size; y++)
    for (int x = 0; x < size; x++)
      if (esp_qrcode_get_module(qr, x, y)) {
        int px = x0 + x * scale, py = y0 + y * scale;
        Paint_DrawRectangle(px, py, px + scale - 1, py + scale - 1,
                            EPD_3IN7G_BLACK, DOT_PIXEL_1X1, DRAW_FILL_FULL);
      }
}

// 横方向中央揃えで英字を描く。
// 色引数はライブラリの Paint_DrawString_EN にそのまま渡す(順序も従来のまま)。
static void drawCenteredEN(int y, const char* s, sFONT* font, UWORD c1, UWORD c2) {
  int w = (int)strlen(s) * font->Width;
  int x = (EPD_3IN7G_WIDTH - w) / 2;
  if (x < 0) x = 0;
  Paint_DrawString_EN(x, y, s, font, c1, c2);
}

static void showSetupScreen() {
  if (!allocBuffer()) return;
  panelOn();
  EPD_3IN7G_Init();
  // 縦置き。回転させず、パネルのネイティブ向き(240x416)にそのまま描く。
  Paint_NewImage(g_image, EPD_3IN7G_WIDTH, EPD_3IN7G_HEIGHT, 0, EPD_3IN7G_WHITE);
  Paint_SetScale(4);
  Paint_SelectImage(g_image);
  Paint_Clear(EPD_3IN7G_WHITE);

  esp_qrcode_config_t cfg = ESP_QRCODE_CONFIG_DEFAULT();
  cfg.display_func       = qrDisplayCallback;
  cfg.max_qrcode_version = 4;
  esp_qrcode_generate(&cfg, QR_PAYLOAD);

  // QRの下に積む。QR領域は最大でも y=235 で終わる(25 + 210)。
  drawCenteredEN(258, "WiFi Setup",          &Font24, EPD_3IN7G_RED,   EPD_3IN7G_WHITE);
  drawCenteredEN(300, "1. Scan this code",   &Font16, EPD_3IN7G_BLACK, EPD_3IN7G_WHITE);
  drawCenteredEN(322, "2. Join EPD-Setup",   &Font16, EPD_3IN7G_BLACK, EPD_3IN7G_WHITE);
  drawCenteredEN(344, "3. Enter your WiFi",  &Font16, EPD_3IN7G_BLACK, EPD_3IN7G_WHITE);
  drawCenteredEN(380, "or open 192.168.4.1", &Font12, EPD_3IN7G_BLACK, EPD_3IN7G_WHITE);

  feedWdt();
  EPD_3IN7G_Display(g_image);
  feedWdt();
  panelOff();
}

static void onPortalStart(WiFiManager* wm) {
  // このコールバックはポータルが開くたびに呼ばれる。
  // ルータが長時間落ちていると
  //   接続失敗(20秒) -> ポータル -> 300秒でタイムアウト -> ESP.restart()
  // が延々と回るため、毎回描いていると約5分半ごとにフル更新が走り続ける。
  // loop()の3分制限はここを通らないので、この経路だけ無防備だった。
  // e-Paperは表示を保持するので、既に設定画面が出ているなら描き直す必要はない。
  if (rtc_setupShown) {
    Serial.println("setup screen already on panel, skip redraw");
    return;
  }
  showSetupScreen();
  rtc_setupShown = 1;
}

// ---------------------------------------------------------------
// HTTP: ロングポーリング
//   200 -> 変化あり(新versionをoutへ)
//   204 -> 変化なし
//   負値/その他 -> エラー
// ---------------------------------------------------------------
static int longPoll(char* out, size_t outLen) {
  HTTPClient http;
  http.setConnectTimeout(CONNECT_TIMEOUT_MS);
  http.setTimeout(POLL_TIMEOUT_MS);
  http.setReuse(false);           // 毎回閉じる。ソケットを溜めない

  String url = String(SERVER_BASE) + "/wait?since=" + String(g_version);
  if (!http.begin(netClient(), url)) { http.end(); netClose(); return -1000; }
  applyAuth(http);

  int code = http.GET();
  if (code == 200) {
    String body = http.getString();
    body.trim();
    if (body.length() == 0 || body.length() >= outLen) code = -1001;
    else strncpy(out, body.c_str(), outLen - 1), out[outLen - 1] = '\0';
  } else if (code == 401) {
    // 認証だけは待っても直らない。ログに理由を残す。
    Serial.println("401: DEVICE_USER/DEVICE_PASS がサーバの設定と合っていない");
  }
  http.end();
  netClose();
  return code;
}

static bool fetchFrame() {
  if (!allocBuffer()) return false;

  g_fetchedVersion[0] = '\0';

  HTTPClient http;
  http.setConnectTimeout(CONNECT_TIMEOUT_MS);
  http.setTimeout(20000);
  http.setReuse(false);

  if (!http.begin(netClient(), String(SERVER_BASE) + "/frame.bin")) {
    http.end();
    netClose();
    return false;
  }
  applyAuth(http);

  // collectHeaders は GET() より前に呼ぶ必要がある
  static const char* kHeaders[] = { "X-Frame-Version" };
  http.collectHeaders(kHeaders, 1);

  int code = http.GET();
  if (code != 200) {
    // 404 はサーバがまだ一度も画像を受け取っていない状態。異常ではない。
    Serial.printf("/frame.bin -> %d\n", code);
    http.end();
    netClose();
    return false;
  }

  String hv = http.header("X-Frame-Version");
  hv.trim();
  if (hv.length() > 0 && hv.length() < sizeof(g_fetchedVersion)) {
    strncpy(g_fetchedVersion, hv.c_str(), sizeof(g_fetchedVersion) - 1);
    g_fetchedVersion[sizeof(g_fetchedVersion) - 1] = '\0';
  }

  int len = http.getSize();
  if (len > 0 && (uint32_t)len != g_imageSize) {
    Serial.printf("size mismatch: %d vs %u\n", len, g_imageSize);
    http.end();
    return false;
  }

  WiFiClient* stream = http.getStreamPtr();
  uint32_t got = 0, idleStart = millis();
  while (got < g_imageSize) {
    feedWdt();
    size_t avail = stream->available();
    if (avail) {
      size_t want = (size_t)(g_imageSize - got);
      size_t n = stream->readBytes(g_image + got, avail < want ? avail : want);
      got += n;
      idleStart = millis();
    } else if (!http.connected()) {
      break;
    } else if (millis() - idleStart > 20000) {
      Serial.println("download stalled");
      break;
    } else {
      delay(2);
    }
  }
  http.end();
  netClose();

  Serial.printf("downloaded %u/%u\n", got, g_imageSize);
  return got == g_imageSize;
}

// ---------------------------------------------------------------
// 描画
// ---------------------------------------------------------------
static void renderFrame() {
  // 一定回数ごと、または24時間ごとに完全初期化してゴーストを消す
  bool full = (g_refreshCount % DEGHOST_EVERY == 0)
              || (uptime() - g_lastFullInitAt > FORCED_REFRESH_MS);

  panelOn();
  feedWdt();
  if (full) {
    Serial.println("full init (deghost)");
    EPD_3IN7G_Init();
    g_lastFullInitAt = uptime();
  } else {
    EPD_3IN7G_Init_Fast();
  }
  feedWdt();
  EPD_3IN7G_Display(g_image);
  feedWdt();
  panelOff();

  g_lastRefreshAt = uptime();
  g_haveRefreshed = true;
  g_refreshCount++;
  // 実コンテンツで上書きしたので、設定画面はもう出ていない。
  // 次にポータルが開いたときは描き直す。
  rtc_setupShown = 0;
}

// ---------------------------------------------------------------
// WiFi監視
// ---------------------------------------------------------------
static bool wifiHealthy() {
  if (WiFi.status() == WL_CONNECTED) {
    g_wifiLostAt  = 0;
    g_wifiRetries = 0;
    return true;
  }

  if (g_wifiLostAt == 0) {
    g_wifiLostAt = uptime();
    Serial.println("wifi lost");
    return false;
  }

  if (uptime() - g_wifiLostAt > WIFI_GRACE_MS) {
    g_wifiRetries++;
    Serial.printf("wifi reconnect attempt %u\n", g_wifiRetries);
    if (g_wifiRetries > MAX_WIFI_RETRY) {
      Serial.println("wifi unrecoverable -> restart");
      delay(200);
      ESP.restart();
    }
    WiFi.disconnect();
    delay(500);
    WiFi.reconnect();
    g_wifiLostAt = uptime();
  }
  return false;
}

// ---------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(300);
  g_lastMillis = millis();

  bool coldBoot = (rtc_magic != RTC_MAGIC);
  if (coldBoot) {
    rtc_magic = RTC_MAGIC;
    rtc_version[0] = '\0';
    rtc_reboots = 0;
    // 電源投入直後はRTC領域が不定。パネルに何が出ているか分からないので、
    // 設定画面は「出ていない」扱いにして、必要なら一度だけ描き直させる。
    rtc_setupShown = 0;
  } else {
    rtc_reboots++;
    strncpy(g_version, rtc_version, sizeof(g_version) - 1);
  }
  Serial.printf("\n=== boot (cold=%d, soft reboots=%u) ===\n", coldBoot, rtc_reboots);
  Serial.printf("reset reason: %d\n", (int)esp_reset_reason());

  // ウォッチドッグ。コア3.x系とAPIが違うので分岐する。
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  esp_task_wdt_config_t wdtCfg = {
    .timeout_ms = WDT_TIMEOUT_S * 1000,
    .idle_core_mask = 0,
    .trigger_panic = true
  };
  esp_task_wdt_reconfigure(&wdtCfg);
#else
  esp_task_wdt_init(WDT_TIMEOUT_S, true);
#endif
  esp_task_wdt_add(NULL);
  feedWdt();

  allocBuffer();

  // 起動直後は必ずパネルの電源を落としておく。
  // ここで panelOff() を呼んではいけない。panelOff() は EPD_3IN7G_Sleep() を
  // 経由するが、まだ一度も EPD_3IN7G_Init() していないパネルは BUSY を
  // 期待どおりに返さず、ライブラリ内の while(BUSY) から抜けられなくなる。
  // setup()の途中で固まり、90秒後にWDTがパニック -> 再起動 -> 同じ場所で
  // 再び停止、という起動ループになる。
  // 電源を切るだけならパネルと通信する必要はなく、PWRピンを下げれば足りる。
  DEV_Module_Init();                // PWR=HIGH
  DEV_Module_Exit();                // PWR=LOW

  // 接続先のスキームを見てTLSを使うか決める。
  g_useTls = (strncmp(SERVER_BASE, "https://", 8) == 0);
  if (g_useTls) {
    g_tls.setInsecure();                  // 証明書は検証しない(上の説明を参照)
    g_tls.setHandshakeTimeout(20);        // 秒。電波が悪いと数秒かかる
  }
  Serial.printf("server: %s (tls=%d, auth=%d)\n",
                SERVER_BASE, (int)g_useTls, (int)(DEVICE_PASS[0] != '\0'));

  WiFi.persistent(true);
  WiFi.setAutoReconnect(true);

  WiFiManager wm;
  wm.setAPCallback(onPortalStart);
  wm.setConfigPortalTimeout(300);
  wm.setConnectTimeout(20);

  // 設定ポータルは最大300秒ブロックし、その間WDTを一度も叩かない。
  // (WiFiManagerの待受ループは handleClient() と delay() を回すだけで、
  //  esp_task_wdt_reset() を呼ばない。delay()のyieldではWDTは解除されない)
  // 購読したままだと利用者がWiFi情報を入力している最中に90秒でpanic resetし、
  // 初回プロビジョニングが永久に完了しない。ポータル中だけ購読を外す。
  esp_task_wdt_delete(NULL);
  bool connected = wm.autoConnect(AP_SSID);
  esp_task_wdt_add(NULL);
  feedWdt();

  if (!connected) {
    Serial.println("provisioning failed -> restart");
    delay(1000);
    ESP.restart();
  }
  Serial.printf("wifi: %s  ip=%s\n",
                WiFi.SSID().c_str(), WiFi.localIP().toString().c_str());

  // 電源投入直後は画面に何が出ているか不明なので、一度必ず取りに行く
  if (coldBoot) g_version[0] = '\0';

  g_lastFullInitAt = uptime();
  feedWdt();
}

// ---------------------------------------------------------------
void loop() {
  feedWdt();
  tickUptime();

  // ---- 計画再起動 ----
  // e-Paperは表示を保持するので、再起動は画面上見えない。
  if (uptime() > PLANNED_REBOOT_MS) {
    Serial.println("planned reboot");
    strncpy(rtc_version, g_version, sizeof(rtc_version) - 1);
    delay(200);
    ESP.restart();
  }

  // ---- ヒープ監視 ----
  uint32_t heap = ESP.getFreeHeap();
  if (heap < HEAP_FLOOR) {
    Serial.printf("heap low (%u) -> restart\n", heap);
    strncpy(rtc_version, g_version, sizeof(rtc_version) - 1);
    delay(200);
    ESP.restart();
  }

  // ---- WiFi ----
  if (!wifiHealthy()) { delay(2000); return; }

  // ---- サーバ問い合わせ ----
  char newVersion[24] = "";
  int code = longPoll(newVersion, sizeof(newVersion));

  bool changed = false;
  if (code == 200) {
    changed = (strcmp(newVersion, g_version) != 0);
    g_serverFailures = 0;
  } else if (code == 204) {
    g_serverFailures = 0;               // 正常な「変化なし」
  } else {
    g_serverFailures++;
    // 指数バックオフ。最大5分。サーバが落ちていても画面は触らない。
    uint32_t backoff = 2000UL << (g_serverFailures > 7 ? 7 : g_serverFailures);
    if (backoff > 300000) backoff = 300000;
    Serial.printf("server error %d (fail=%u), backoff %ums\n",
                  code, g_serverFailures, backoff);
    uint32_t t0 = millis();
    while (millis() - t0 < backoff) { feedWdt(); delay(500); }
    return;
  }

  // ---- 焼付防止の強制更新 ----
  bool forced = g_haveRefreshed && (uptime() - g_lastRefreshAt > FORCED_REFRESH_MS);
  if (!changed && !forced) return;

  // ---- 更新間隔の下限を強制 ----
  // サーバ側ではなくここで守る。サーバを差し替えても保護が消えない。
  while (g_haveRefreshed && (uptime() - g_lastRefreshAt < MIN_REFRESH_GAP_MS)) {
    Serial.println("rate limited, holding...");
    feedWdt();
    delay(5000);
  }

  if (changed) {
    if (!fetchFrame()) { delay(5000); return; }

    // 記録するのは longPoll が返した版ではなく、実際に受け取った中身の版。
    // 上のレート制限で最大180秒待つ間にサーバが更新されると、
    // ダウンロードされるのは最新フレームなのに newVersion は古いままになる。
    // それを記録すると次の周回で「まだ変化がある」と誤判定し、
    // 同じ内容をもう一度フル描画してしまう(パネル寿命を無駄に削る)。
    const char* applied = g_fetchedVersion[0] ? g_fetchedVersion : newVersion;
    strncpy(g_version, applied, sizeof(g_version) - 1);
    g_version[sizeof(g_version) - 1] = '\0';
    strncpy(rtc_version, g_version, sizeof(rtc_version) - 1);
    rtc_version[sizeof(rtc_version) - 1] = '\0';
  }
  // forcedのみの場合は既存バッファをそのまま描き直す(内容は同じ、焼付防止が目的)

  renderFrame();
  Serial.printf("refreshed #%u  uptime=%llus  heap=%u\n",
                g_refreshCount, uptime() / 1000, ESP.getFreeHeap());
}
