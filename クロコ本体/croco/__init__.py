"""クロコ：Notion + Gemini + Claude Code による個人用ワークフロー自動化パイプライン。

仕様は `クロコ_仕様書_統合版.md` を参照。
外部パッケージには依存しない（標準ライブラリのみ）。PC起動時に走らせる都合上、
pip や仮想環境の状態に実行可否を左右されないようにするため。
"""

__all__ = ["config", "httpjson", "notion", "gemini", "capture", "dispatch"]
