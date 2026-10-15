# WinResMon

Windows のリソース状況を可視化するデスクトップアプリです。

- **リアルタイム**：CPU（全体・コア別）、メモリ、ネットワーク送受信、ディスク I/O、上位プロセス、ドライブ使用率
- **ストレージ**：任意の時点でフォルダ容量をスキャンしてスナップショット保存。ツリーマップで閲覧し、別時点との増減を比較

## インストール

[Releases](../../releases) から `WinResMon-Setup-x.y.z.exe` をダウンロードして実行します。

- 既定では管理者権限なしで「自分だけ」にインストールされます
- 起動するとアプリ専用のウィンドウで開きます（Windows 10/11 標準の WebView2 を使用）
- スタートメニューの「WinResMon（ブラウザで開く）」を選ぶと、既定のブラウザで開きます
- スナップショットは `%LOCALAPPDATA%\WinResMon\snapshots` に保存されます。アンインストールしても削除されません

> 署名されていないため、初回起動時に SmartScreen の警告が出ることがあります。「詳細情報」→「実行」を選んで起動してください。

## リリース手順

```
git tag v1.0.0
git push origin v1.0.0
```

タグを push すると GitHub Actions が次の順に処理し、成功すると Release にインストーラーが添付されます。

1. PyInstaller で exe をビルド
2. exe の自己テスト
3. Inno Setup でインストーラーを作成
4. サイレントインストール → 自己テスト → アンインストールで確認

`main` ブランチへの push でもビルドは実行されます。この場合、インストーラーは Actions の実行結果ページの Artifacts からダウンロードできます。

## 開発時の起動

```
pip install -r requirements.txt
python app.py             # 専用ウィンドウで開く
python app.py --browser   # ブラウザで開く
python app.py --selftest  # 動作確認だけして終了
```
