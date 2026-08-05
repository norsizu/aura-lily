# Aura Lily

[简体中文](README.md) | [English](README.en.md) | [日本語](README.ja.md)

> デスクの上で暮らすオープンソース音声コンパニオン。聞き、話し、状態を表示し、自分の一日を過ごします。

Aura Lily は Waveshare ESP32-S3-RLCD-4.2 向けに作られています。小さな画面にチャットを押し込んだだけのものではありません。音声会話、キャラクターの状態、シーン、日々のリズムを、セルフホストできる ESP32-S3 デバイスにまとめます。端末は録音、音声、表示、ローカル操作を担当し、手元のサーバーが音声処理、モデル呼び出し、任意の世界状態を担当します。

## プロジェクトの位置づけ

Aura Lily は、個人のセルフホストと ESP32-S3 ハードウェア向けにまとめた Aura の単体運用版・軽量オープンソース版です。音声会話、キャラクターの状態、シーン、予定、設定可能なゲートウェイという中心体験を残し、完全版 Aura に含まれる非公開の本番サービスと一部の高度な機能は含みません。完全版 Aura は本リポジトリでオープンソース公開する予定はありません。

## デモ

<p align="center">
  <a href="docs/media/AuraHero-v13-full-english-answer.mp4">
    <img src="docs/media/AuraHero-v13-cover.jpg" width="320" alt="Aura Lily device demo">
  </a>
</p>

<p align="center">
  <a href="https://youtube.com/shorts/B0PyZoMU2sw">YouTube Shorts で見る</a> · <a href="docs/media/AuraHero-v13-full-english-answer.mp4">MP4 をダウンロード</a>
</p>

<video controls preload="metadata" poster="https://raw.githubusercontent.com/norsizu/aura-lily-public/main/docs/media/AuraHero-v13-cover.jpg" width="640">
  <source src="https://raw.githubusercontent.com/norsizu/aura-lily-public/main/docs/media/AuraHero-v13-full-english-answer.mp4" type="video/mp4">
  <a href="docs/media/AuraHero-v13-full-english-answer.mp4">デモ動画を開く</a>
</video>

## ハードウェアとクレジット

Aura Lily は Waveshare の [ESP32-S3-RLCD-4.2 開発ボード](https://docs.waveshare.net/ESP32-S3-RLCD-4.2/)をベースにしています。筐体は [黄木匠（Siagfried）](https://makerworld.com.cn/zh/@Siagfried) が制作しました。参考モデルは [4.2 インチ反射型ディスプレイ用筐体](https://makerworld.com.cn/zh/models/2726139-wei-xue-4-2cun-quan-fan-she-ping-kai-fa-ban-wai-ke#profileId-3216633)です。

<table>
  <tr>
    <td><img src="docs/media/hardware/waveshare-esp32-s3-rlcd-4.2.webp" width="220" alt="Waveshare ESP32-S3-RLCD-4.2"></td>
    <td><img src="docs/media/posters/aura-braun.jpg" width="155" alt="Aura Lily Braun palette"></td>
    <td><img src="docs/media/posters/aura-pixel-green.jpg" width="155" alt="Aura Lily natural green pixel palette"></td>
    <td><img src="docs/media/posters/aura-famicom.jpg" width="155" alt="Aura Lily Nintendo palette"></td>
    <td><img src="docs/media/posters/aura-macintosh.jpg" width="155" alt="Aura Lily Macintosh palette"></td>
  </tr>
  <tr>
    <td align="center">元の開発ボード</td>
    <td align="center">Braun</td>
    <td align="center">ナチュラルグリーン</td>
    <td align="center">ファミコン</td>
    <td align="center">Macintosh</td>
  </tr>
</table>

## ふつうの音声アシスタントとの違い

- **会話は状態とつながっています。** Aura には気分、体力、満腹度、ストレス、好感度、豆子があります。会話、食事、休息、買い物、予定の完了によって状態の一部が変化します。
- **一日は固定スクリプトではありません。** 世界レイヤーには起床・三食・就寝前の整理という 5 つの生活アンカーがあり、時刻、天気、気分、体力、満腹度、ストレス、好感度、所持金から 4〜8 個の動的な活動を作ります。
- **言語は一貫した経路です。** 中国語、英語、日本語で、UI、音声認識結果、返答、TTS 出力が現在の会話言語に合わせて動作します。
- **端末そのものが体験です。** 400 x 300 の反射型 1-bit 画面に、キャラクター、服装、シーン、字幕、状態、情報ボードを表示します。短いローカル案内音声のために、毎回 TTS を呼ぶ必要はありません。

## 含まれる機能

| 領域 | 内容 |
| --- | --- |
| 音声ターン | 端末録音、Opus アップリンク、ASR、ストリーミング応答、TTS 音声の返送。字幕は実際の音声再生に合わせて進みます。 |
| 3 言語 | 中国語、英語、日本語の UI と音声経路。利用枠の案内もローカライズされます。 |
| 日常世界 | 任意で有効にできる状態、予定、世界レイヤー。食事、休息、外出、買い物の予定は実際の状態変化として精算されます。 |
| ローカル接続 | 実際の SSID 名を表示する 2 つの保存済み Wi-Fi スロットと手動切り替え。 |
| OTA | デュアルアプリ領域、アプリ/アセット OTA、SHA-256 検証、起動ロールバック。古い単一パーティション端末は最初の OTA 移行前に一度だけ有線で完全書き込みが必要です。 |
| セルフホスト | Hermes、会話モデル、ASR、TTS、会話枠、任意の Soul を設定するローカル管理 UI。ファームウェアには既定のサーバー接続先を含みません。 |

## 構成

```text
ESP32-S3 device
  microphone / buttons / RLCD / speaker
            | WebSocket
            v
Aura Lily gateway
  ASR -> conversation model -> TTS
            |
            +-- optional Aura state and daily-world layer
```

サービスは自分で管理できる PC、NAS、またはサーバーで動かします。モデルと音声プロバイダーは自分で選択・設定します。端末には LAN、Tailscale、または公開アドレスを設定し、`127.0.0.1` は使いません。

## クイックスタート

### 1. ローカルサービスを起動する

必要なものは Python 3.11+、Docker Compose、設定済みの `hermes` CLI または OpenAI-compatible モデル API です。ファームウェアのビルドには ESP-IDF 5.x が必要です。

```bash
cp .env.example .env
docker compose up --build
```

別のターミナルで確認します。

```bash
curl -s http://127.0.0.1:8765/health
```

HTTP/管理画面の既定ポートは `8765`、ESP32 WebSocket ゲートウェイは `8787` です。`http://<your-server-address>:8765/admin` でモデル、ASR、TTS、管理者パスワードを設定します。モデルの認証情報はローカルの実行環境にのみ置き、このリポジトリからは提供されません。

### 2. 任意の世界レイヤーを有効にする

基本の Hermes ブリッジは単独で動作します。Aura の状態、シーン、予定を有効にするには `.env` に次を設定します。

```bash
AURA_PERSONA_ENABLED=1
```

Soul は空の状態で始まります。ローカル管理 UI から自分で入力するか、`.docker/aura-persona/persona/soul.md` を作成してください。状態と予定は Git で無視されるローカルの実行データに保存されます。

### 3. 端末をビルドして書き込む

```bash
cd firmware/esp32
source "$HOME/esp/esp-idf/export.sh"
idf.py set-target esp32s3
idf.py menuconfig
idf.py build
idf.py -p /dev/cu.usbmodemXXXX flash monitor
```

`menuconfig > Aura Lily` で自分の WebSocket と OTA マニフェスト URL を設定するか、初回起動時のプロビジョニング画面から保存します。`127.0.0.1` ではなく、LAN、Tailscale、または公開アドレスを使ってください。

### 4. Wi-Fi と OTA

接続に成功した Wi-Fi は 2 件まで保存され、端末メニューで SSID が表示されます。既定の OTA サーバーはありません。`menuconfig > Aura Lily` に自分の HTTPS マニフェスト URL を設定し、`tools/make_ota_release.py` でファームウェアとアセットのマニフェストを作成してください。`manifest.json` を公開する前に、すべての成果物を先にアップロードします。

Hermes ブリッジの HTTP 契約とスモークテストの詳細は [Hermes bridge guide](integrations/hermes_lily_cli/README.md) を参照してください。

## リポジトリ構成

```text
firmware/esp32/                     ESP32-S3 firmware, display, audio and local assets
integrations/hermes_lily_cli/       Hermes bridge, HTTP/WS gateway and local admin UI
integrations/aura_persona_gateway/  Optional Aura state, reminders, weather and world schedule
tests/                              Focused gateway, world, Wi-Fi, OTA and quota tests
tools/                              Asset, voice, diagnostics and OTA release tools
```

## 設定の安全性

このリポジトリにはモデルのキー、既定のサービス接続先、個人のキャラクター設定は含まれていません。`.env`、`.docker/`、端末バックアップ、ビルド成果物は自分のプライベート環境で管理してください。ナレッジベースが必要な場合は、自分のサーバーで追加できます。

## 検証

```bash
python3 -m pytest -q
```

ファームウェアを公開する前には `idf.py build` も実行し、アプリケーションイメージが `0x280000` の OTA パーティションに収まることを確認してください。

## コミュニティ

ハードウェア移植、導入、キャラクターアセット、セルフホスト運用についての交流は「闲话 AI | Aura」QQ グループへ。グループ番号は `951895791` です。

<p align="center">
  <img src="docs/community/qq-group.jpg" width="250" alt="Xianhua AI Aura QQ group 951895791">
</p>
