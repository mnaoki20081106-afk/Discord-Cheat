"""Discord-Cheat hub: 3ゲーム(にゃんこ/ツムツム/ぷにぷに)の代行Botを1プロセスにまとめる管理層。

- store        : サーバーごとのパネル設置先/色/実績設定(JSON)
- panels       : 3ゲームのパネル(Embed+永続View)の生成と設置・更新
- achievements : 完了実績の投稿(Discord-Shiireの購入ログ方式)
- api          : Xaccount-Botの管理画面から叩かれるHMAC署名付きBridge API
"""

VERSION = "1.0.0"
