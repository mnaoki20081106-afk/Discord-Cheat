# Discord-Cheat

にゃんこ大戦争 / ツムツム / ぷにぷに の自動代行を **1つのDiscord Bot・1プロセス** で動かし、
Xaccount-Bot の管理サイト「代行管理」タブからパネルの設置先・色・実績の投稿先を操作できるようにしたものです。

```text
管理サイト「代行管理」 ──▶ Xaccount-Bot (Cloudflare Worker + D1)  ◀── 約10秒ごとに問い合わせ(HTTPSの外向きのみ)
                                                                     │
                                                          Discord-Cheat (Python常駐)
                                                          ├ games/nyanko  元の Cogs をそのまま読込
                                                          ├ games/tsum    mod_stum + hub/tsum_cog.py
                                                          └ games/puni    ywp_bot.py (元の bot.py)
```

**外部公開は不要です。** ドメイン・Tunnel・ポート開放なしで、自宅や無料ホストでも動きます。
管理サイトで設定した内容と「パネルを設置」「テスト送信」の依頼は Xaccount-Bot の D1 に溜まり、
Bot が取りに行って実行→結果を返します(反映まで最大10秒ほど)。

## 構成

| パス | 内容 |
|---|---|
| `main.py` | 起動。`.env` を読み、3ゲームを1つのBotに載せる |
| `hub/store.py` | サーバーごとのパネル/実績設定(`data/hub.json`) |
| `hub/puller.py` | Xaccount-Botへの問い合わせ(設定の取り込み・依頼の実行・状態報告) |
| `hub/panels.py` | 3ゲームのパネル(Embed+永続View)の生成・設置・更新 |
| `hub/achievements.py` | 完了実績の投稿 |
| `games/` | 元のBotのコード(変更は実績の呼び出しとフック追加、省メモリ設定のみ) |
| `scripts/setup.py` | `.env` を作り、`CHEAT_BRIDGE_SECRET` を乱数生成 |
| `tests/` | `python -m unittest discover -s tests` |

## セットアップ

```bash
pip install -r requirements.txt
python scripts/setup.py          # .env 作成 + CHEAT_BRIDGE_SECRET を自動生成して表示
# .env に TOKEN / OWNER_ID / XACCOUNT_BOT_URL を入れる
MALLOC_ARENA_MAX=2 python main.py
```

`MALLOC_ARENA_MAX=2` は省メモリ用(400MBクラスのホスト向け)。待機時は約100MB、ジョブ実行中は増えます。

Bot-Factory からは m-ws プロバイダ(`bot-factory.json` の `provider: "mws"`)で起動します。
環境変数(`TOKEN` / `OWNER_ID` / `XACCOUNT_BOT_URL` / `CHEAT_BRIDGE_SECRET` / `MALLOC_ARENA_MAX=2` など)は m-ws のサイト側で設定します。
Factory を使わない場合は、常時起動できるマシン/VPSで `python main.py` を動かします。

### Developer Portal
- Bot Token を発行(特権インテントは不要。`Intents.default()` で動作)
- 招待スコープ `bot` + `applications.commands`
- Bot権限: チャンネルを見る / メッセージを送信 / 埋め込みリンク / メッセージ履歴を読む

### Xaccount-Bot と繋ぐ
1. `python scripts/setup.py` が表示した `CHEAT_BRIDGE_SECRET` を、Bot-Factory の Xaccount-Bot フォーム
   「Discord-Cheat連携 Secret」へ貼って再デプロイ。
2. `.env` の `XACCOUNT_BOT_URL` に Xaccount-Bot の URL(管理サイトの `VITE_API_BASE_URL` と同じもの)を入れる。
3. Bot を起動し、対象サーバーへ招待 → 管理サイト「代行管理」タブを開く。
   最初は「接続待ち」と出て、Botが状態を報告するとパネル設定が現れます。

Bot-Factory は Cloudflare Worker 専用なので、このPythonのBotは Factory では起動できません。
常時起動できる場所(PC/VPS/Botホスティング)で動かします。

## 代行管理タブでできること
- **パネル設置**(にゃんこ / ツムツム / ぷにぷに): チャンネルと色を選んで設置。同じチャンネルなら既存メッセージを更新、別チャンネルなら新規設置して古いものを削除。
- **パネルの色**: 既定は元のBotの色。管理画面で変更、既定色へ戻せる。
- **実績**: 3ゲームの完了をまとめて1チャンネルへ(ゲームごとの上書き・ON/OFF可)。Discord-Shiireの購入完了ログと同じくメンションなし。金額/ご依頼者の表示切替、色、テスト送信。

実績が出るタイミング: にゃんこ=有料注文の成功(テスト販売機は対象外) / ツムツム=1メニュー以上成功 / ぷにぷに=元の `post_result`(無料・有料)。

## 通信と安全
- Bot → Worker の `POST /api/cheat-bridge/{poll,report,result}` のみ。署名ヘッダ `X-Cheat-Timestamp`(ms) /
  `X-Cheat-Signature` = `HMAC-SHA256(secret, timestamp \n METHOD \n path+query \n body)`。許容ずれ5分。
- Secret が一致しない/未設定なら Worker は 401/503 を返し、Bot側はログを出して再試行を続けます(Bot本体は動作継続)。
- 取りに行く間隔は `hub/config.py` の `POLL_SECONDS`(既定10秒)。Cloudflare無料枠(D1書き込み/リクエスト数)に収まる想定です。
- `.env` と `data/` はGit管理外です。

## 運用メモ
- 再起動時に自動で立ち上がるようにしてください(systemd / ホスティングの自動再起動など)。
- ぷにぷに: プロキシを使うなら `games/puni/proxy.txt`(`proxy.txt.example` 参照)。有料版は `/paypayログイン1` `/paypayログイン2` で受取口座にログイン。
- にゃんこ: 受取用PayPayは `/paypay登録`。価格は `games/nyanko/Cogs/Account_vending.py` の `PRICES`。
- 元の `/実績チャンネル設定`(ぷにぷに)は、ハブ運用では管理画面へ案内するだけの表示になります。
- 元の `/vending_account` は押した人ではなくコマンド実行者へDMする作りでした。常設パネルは「押した人」宛に作り直しています(`hub/panels.py`)。`/vending` 系は元のままです。

## m-ws で動かすときの注意
- m-ws はプロキシ系ライブラリ(`aiohttp-socks` / `PySocks` など)をブロックします。`requirements.txt` から外してあり、ぷにぷにの SOCKS プロキシ回し(`proxy.txt`)は無効です(使おうとすると案内エラーを出します)。
