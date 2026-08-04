"""捕捉フェーズ：「未処理置き場」→ Gemini で分割 → Inbox DB → 「処理済み置き場」。

仕様書2.5章-3のフローをそのまま実装したもの。

冪等性の考え方（この実装の要）:
「未処理置き場」の子ページとして存在していること自体が未処理の印である。
したがって **登録に成功したページだけを移動させる**。途中で失敗したものは
移動させずに放置するだけでよく、次回のPC起動時に自然に再試行される。
別途クラッシュ復旧の仕組みを持たない代わりに、「失敗時は何もしない」を
デフォルトの挙動にすることで一貫性を保つ。
"""

from __future__ import annotations

from .config import Config
from .gemini import Gemini
from . import dedupe, genre, inbox, log
from . import notion as nt


def run(config: Config, notion: Gemini | None = None) -> int:
    """捕捉フェーズを実行し、Inbox DBに登録した件数を返す。"""
    client = nt.Notion(config.notion_token, config.notion_version)
    gemini = Gemini(
        config.gemini_api_key,
        model=config.gemini_model,
        thinking_level=config.gemini_thinking_level,
        temperature=config.gemini_temperature,
    )

    data_source_id = config.inbox_data_source_id
    if not data_source_id:
        data_source_id = client.resolve_data_source_id(config.inbox_database_id)
        log.log(f"データソースIDを解決しました: {data_source_id}")
        log.log(
            "  毎回の解決を省くには .env に "
            f"NOTION_INBOX_DATA_SOURCE_ID={data_source_id} を追記してください。"
        )

    sources = client.list_child_pages(config.unprocessed_page_id)
    if not sources:
        log.log("未処理置き場は空でした。")
        return 0

    log.log(f"未処理のメモが {len(sources)} 件あります。")

    registered_total = 0
    for source in sources:
        registered_total += _process_one(
            client, gemini, config, source, data_source_id
        )
    return registered_total


def _process_one(
    client: nt.Notion,
    gemini: Gemini,
    config: Config,
    source: dict,
    data_source_id: str,
) -> int:
    """メモ1件を処理する。失敗した場合、そのページは移動させずに残す。"""
    page_id = source["id"]
    label = source["title"] or page_id

    try:
        page = client.get_page(page_id)
        # Notionが記録した正確な作成日時。これを「起草日時」として使う。
        captured_at = page.get("created_time", "")
        raw_log = client.get_page_text(page_id)
    except Exception as exc:
        log.error(f"[{label}] 読み取りに失敗しました。次回に持ち越します: {exc}")
        return 0

    if not raw_log.strip():
        log.warn(f"[{label}] 本文が空のため、移動せずそのままにします。")
        return 0

    try:
        items = gemini.split_raw_log(raw_log, captured_at=captured_at)
    except Exception as exc:
        log.error(f"[{label}] Geminiの分割に失敗しました。次回に持ち越します: {exc}")
        return 0

    if not items:
        log.warn(f"[{label}] 分割結果が0件でした。移動せずそのままにします。")
        return 0

    if config.dry_run:
        log.log(f"[{label}] （dry-run）{len(items)}件に分割されました:")
        for item in items:
            log.log(f"    - [{item['kind']}] {item['title']}")
        return 0

    # 1件でも登録に失敗したら移動しない。
    # 移動しなければ次回まるごと再試行されるだけで済む。
    registered = 0
    for item in items:
        try:
            duplicate = None
            if item["kind"] == inbox.KIND_SCHEDULE:
                duplicate = dedupe.find_duplicate(
                    client,
                    gemini,
                    config,
                    data_source_id=data_source_id,
                    title=item["title"],
                    body=item["body"],
                    scheduled_date=item.get("scheduled_date", ""),
                )
            if duplicate:
                dedupe.merge_into(
                    client, duplicate, new_title=item["title"], new_body=item["body"]
                )
                log.log(
                    f"[{label}] 「{item['title']}」は既存の「{duplicate.title}」"
                    f"({duplicate.id}) と重複と判定し統合しました。"
                )
            else:
                item_genre = genre.assign_for_capture(
                    client,
                    gemini,
                    data_source_id=data_source_id,
                    title=item["title"],
                    body=item["body"],
                )
                client.create_page(
                    data_source_id=data_source_id,
                    properties=inbox.build_properties(
                        item, spoken_at=captured_at, genre=item_genre
                    ),
                    children=nt.paragraph_blocks(item["body"]),
                )
            registered += 1
        except Exception as exc:
            log.error(
                f"[{label}] Inbox登録に失敗しました（{registered}/{len(items)}件目まで成功）。"
                f" このメモは移動させず残します: {exc}"
            )
            return registered

    try:
        client.move_page(page_id, new_parent_page_id=config.processed_page_id)
        log.log(f"[{label}] {registered}件を登録し、処理済み置き場へ移動しました。")
    except Exception as exc:
        # 登録は済んでいるので、ここで失敗すると次回に重複登録が起きうる。
        # 自動では直せないため、はっきり警告して人間の判断に委ねる。
        log.error(
            f"[{label}] Inbox登録は成功しましたが、処理済み置き場への移動に失敗しました。"
            f" **次回起動時に重複登録される可能性があります**。"
            f" 手動でこのページを処理済み置き場へ移動してください: {exc}"
        )

    return registered
