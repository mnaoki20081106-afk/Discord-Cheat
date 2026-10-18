# Discord-Cheat

にゃんこ大戦争 / ツムツム / ぷにぷに の自動代行を **1つのDiscord Bot・1プロセス** で動かし、
Xaccount-Bot の管理サイト「代行管理」タブからパネルの設置先・色・実績の投稿先を操作できるようにしたものです。

```text
Xaccount-Bot (Cloudflare Worker + 管理サイト)
   「代行管理」タブ ──HMAC署名──▶  Discord-Cheat  hub/api.py  (Bridge API)
                                      │ data/hub.json  (設置先 / 色 / 実績設定)
                                      ▼
                          ┌── games/nyanko   (元の Cogs をそのまま読込)
          1つのBot ───────┼── games/tsum     (mod_stum + hub/tsum_cog.py)
                          └── games/puni     (ywp_bot.py = 元の bot.py)
```

## 構成

| パス | 内容 |
|---|---|
| `main.py` | 起動。`.env` を読み、3ゲームを1つのBotに載せる |
| `hub/` | 管理層: `store`(設定JSON) / `panels`(パネル生成・設置・更新) / `achievements`(実績投稿) / `api`(Bridge API) |
| `games/nyanko` `games/tsum` `games/puni` | 元のBotのコード。変更は完了実績の呼び出しとフック追加のみ |
| `scripts/setup.py` | `.env` を作り、`CHEAT_BRIDGE_SECRET` を乱数生成 |
| `tests/` | `python -m unittest discover -s tests` |

## セットアップ

```bash
pip install -r requirements.txt
python scripts/setup.py          # .env を作成し CHEAT_BRIDGE_SECRET を自動生成して表示
# .env に TOKEN / OWNER_ID を入れる
python main.py
```

Bot-Factory は Cloudflare Worker 専用(`runtime: worker`)なので、PythonのこのBotは Factory では起動できません。
常時起動できるマシン/VPSで `python main.py` を動かします。

### Developer Portal
- Bot Token を発行(権限インテントは不要。`Intents.default()` で動作)
- 招待スコープ `bot` + `applications.commands`
- Bot権限: チャンネルを見る / メッセージを送信 / 埋め込みリンク / メッセージ履歴を読む

### Xaccount-Bot と繋ぐ
1. `python scripts/setup.py` が表示した `CHEAT_BRIDGE_SECRET` を、Bot-Factory の Xaccount-Bot フォーム
   「Discord-Cheat連携 Secret」へ貼る(Xaccount-Bot側の `CHEAT_BRIDGE_SECRET`)。
2. Bridge API (`CHEAT_API_HOST:CHEAT_API_PORT`, 既定 `127.0.0.1:8790`) をHTTPSで公開する
   (Cloudflare Tunnel / リバースプロキシ)。そのoriginを「Discord-Cheat Bridge URL」(`CHEAT_API_BASE_URL`)へ入れる。
   `CHEAT_BRIDGE_SECRET` が未設定(32文字未満)だとAPIは起動しません。
3. Discord-Cheat BOT を対象サーバーへ招待 → 管理サイト「代行管理」タブを開く。

## 代行管理タブでできること
- **パネル設置**(にゃんこ / ツムツム / ぷにぷに): チャンネルと色を選んで設置。同じチャンネルなら既存メッセージを更新、別チャンネルなら新規設置して古いものを削除。
- **パネルの色**: 既定は元のBotの色。管理画面で変更、既定色へ戻せる。
- **実績**: 3ゲームの完了をまとめて1チャンネルへ(ゲームごとの上書き・ON/OFF可)。Discord-Shiireの購入完了ログと同じく、メンションなし。金額/ご依頼者の表示切替、色、テスト送信。

実績が出るタイミング: にゃんこ=有料注文の成功(テスト販売機は対象外) / ツムツム=1メニュー以上成功 / ぷにぷに=元の `post_result`(無料・有料)。

## Bridge API (HMAC-SHA256)
ヘッダ `X-Cheat-Timestamp`(ms) / `X-Cheat-Nonce`(1回限り) / `X-Cheat-Signature` =
`HMAC(secret, timestamp \n nonce \n METHOD \n path+query \n body)`。許容ずれ5分、nonceは再利用不可。

```text
GET  /bridge/main/status
GET  /bridge/main/guilds/:gid/config
PUT  /bridge/main/guilds/:gid/config
POST /bridge/main/guilds/:gid/panels/:game          (nyanko|tsum|puni)  {channelId, color?}
POST /bridge/main/guilds/:gid/achievements/test     {game}
```

## 運用メモ
- `data/`(設定・にゃんこの実行時JSON)と `.env` はGit管理外。バックアップするなら `data/` を。
- 元の `/実績チャンネル設定`(ぷにぷに)は、ハブ運用では管理画面へ案内するだけの表示になります。
- 元の `/vending_account` は押した人ではなくコマンド実行者へDMする作りでした。常設パネルは「押した人」宛に作り直しています(`hub/panels.py`)。`/vending` 系は元のままです。
