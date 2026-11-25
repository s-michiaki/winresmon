# WinResMon

Windows のリソース状況を可視化するデスクトップアプリです。

- **リアルタイム**：CPU（全体・コア別）、メモリ、GPU（使用率・VRAM・プロセス別、NVIDIA は温度・電力も）、ネットワーク送受信、ディスク I/O、上位プロセス、ドライブ使用率
- **ストレージ**：任意の時点でフォルダ容量をスキャンしてスナップショット保存。ツリーマップで閲覧し、別時点との増減を比較
  - フォルダ・ファイルを選んで削除（既定はごみ箱へ移動。完全削除は確認のうえで実行。Windows やアプリに必要な場所は削除できません）
- **通信**：どのアプリが、どこと、どれくらい通信しているかをリアルタイム表示（旧 NetWatch を統合）
  - 接続一覧（TCP / UDP・IPv4 / IPv6）：プロセス・PID・宛先・状態・ホスト名・送受信速度と累計。プロセスで絞り込み、検索
  - 通信先の記録：「● 記録開始」〜「■ 停止して保存」の間に通信した宛先を集計し、CSV（Excel 対応）/ JSON で保存。記録するプロセスを複数選択可
  - 国別マップ：宛先 IP の国を判定し、どの国と多く通信しているかを世界地図とランキングで表示

## インストール

[Releases](../../releases) から `WinResMon-Setup-x.y.z.exe` をダウンロードして実行します。

- 管理者権限は不要です（常に「自分だけ」にインストールされます）
- C:\ 全体を漏れなくスキャンしたいときは、スタートメニューの「WinResMon（管理者として起動）」か、アプリ内の「管理者として再起動」ボタンを使ってください
- 起動するとアプリ専用のウィンドウで開きます（Windows 10/11 標準の WebView2 を使用）
- スタートメニューの「WinResMon（ブラウザで開く）」を選ぶと、既定のブラウザで開きます
- スナップショットは `%LOCALAPPDATA%\WinResMon\snapshots` に保存されます。アンインストールしても削除されません

> 署名されていないため、初回起動時に SmartScreen の警告が出ることがあります。「詳細情報」→「実行」を選んで起動してください。

## 通信タブについて

- 接続一覧は通常権限でも表示できます。**通信量（↑↓）・ドメイン名・短命な接続の検出**には管理者権限が必要です（画面の「🛡 管理者として再起動」か、スタートメニューの「WinResMon（管理者として起動）」）
- 管理者権限のときは、同梱の補助プログラム `WinResMonNetAgent.exe` が ETW（Event Tracing for Windows）で通信量と DNS 名を取得します。WinResMon を閉じると一緒に終了します
- 記録は `%LOCALAPPDATA%\WinResMon\net_records` に保存されます。記録対象の選択は次回起動時も引き継がれます
- 記録と国別の集計は、画面の表示フィルタとは関係なく、ループバック以外のすべての通信を対象にします
- 国の判定には [DB-IP](https://db-ip.com) の IP to Country Lite（CC BY 4.0）を同梱しています。「GeoIP データベースを更新」で最新版を `%LOCALAPPDATA%\WinResMon` にダウンロードできます
- 国の判定は IP アドレスの登録国です。CDN や Anycast（Cloudflare・Google など）の IP は、実際に通信しているサーバーの場所と異なることがあります
- 地図データ：Made with [Natural Earth](https://www.naturalearthdata.com)（パブリックドメイン）。ライセンス表記は `netdata/THIRD-PARTY-NOTICES.txt`

## リリース手順

GitHub の **Actions → Build Windows installer → Run workflow** を開き、`release` にバージョン（例: `1.1.0`）を入力して実行します。
ビルド・自己テスト・インストールテストがすべて通ると、タグ `v1.1.0` と Release が作られ、インストーラーが添付されます。

`v1.1.0` のようなタグを push しても同じように Release が作られます。
`main` への push ではテストビルドだけを行います。インストーラーは実行結果ページの Artifacts からダウンロードできます。
リリースノートは `RELEASE_NOTES.md` の内容が使われるので、リリース前に更新してください。

## 開発時の起動

```
pip install -r requirements.txt
dotnet publish netagent/WinResMonNetAgent.csproj -c Release -o netagent/publish   # 通信タブの補助プログラム（.NET 8 SDK が必要）
python app.py             # 専用ウィンドウで開く
python app.py --browser   # ブラウザで開く
python app.py --selftest  # 動作確認だけして終了（管理者で実行すると通信量・DNS の取得も確認）
```

| ファイル | 内容 |
|---|---|
| `app.py` | HTTP サーバー・API・自己テスト |
| `netmon.py` | 通信タブ（接続一覧・記録・国別集計・GeoIP） |
| `netagent/` | 通信量・DNS 名を ETW で取る補助プログラム（C#） |
| `netdata/` | GeoIP データベースとライセンス表記 |
| `static/world.json` | 国別マップの世界地図（Natural Earth） |
