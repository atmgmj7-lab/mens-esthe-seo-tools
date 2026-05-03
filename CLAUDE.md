# mens-esthe-seo-tools — Claude Code 動作ルール

## 共通ルール参照

> 共通のトークン効率・Git安全運用・セキュリティルールは `@../CLAUDE.md` を参照すること。

## mens-esthe-seo-tools 固有ルール

- `mcp-server-fetch` は npm パッケージでは存在しない。`uvx mcp-server-fetch` を使用すること。
- FTP デプロイは GitHub Actions で自動化済み。手動 FTP 操作は行わないこと。
- SEO キーワード戦略の変更前は `@docs/seo-strategy.md` を参照すること。
- セッション開始時は `@docs/tasks.md` を確認してから作業を開始すること。

## 実装プロセス

- **3ファイル以上の変更・設計判断が必要な場合は `/plan` モードで計画を提示してから実装すること**。
- 大規模タスクは小さなステップに分割し、1ステップずつ承認を得て進めること。

## Git安全運用

- **`git push --force` および `--force-with-lease` は絶対に使用しないこと。**

## セキュリティ

- APIキー・シークレットは絶対にチャットUIに貼り付けないこと。
- `.env` ファイルは `.gitignore` に含まれていることを常に確認すること。
