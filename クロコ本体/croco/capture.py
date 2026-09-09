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


def _merge_ideas(items: list[dict]) -> list[dict]:
    """種別＝アイデアの分割結果を1件に統合する。

    現状の運用では、1つのメモは1つの実装について話している
    （2026-08-08、本人の指摘）。話題の切れ目で機械的に分割すると、
    同じ1つの実装に対する複数の要件が別々のInboxアイテムに割れ、
    実装フェーズが1件ずつバラバラに着手して他の要件の存在に気づけなくなる。
    予定・資料は性質が違うので混ぜず、対象は種別＝アイデアのみ。
    """
    idea_positions = [i for i, item in enumerate(items) if item["kind"] == inbox.KIND_IDEA]
    if len(idea_positions) <= 1:
        return items

    ideas = [items[i] for i in idea_positions]
    merged = dict(ideas[0])
    merged["body"] = "\n\n".join(idea["body"] for idea in ideas)
    # 本人対応が要る理由は、どれか1件にでも付いていたら引き継ぐ
    # （統合のどさくさで「なし」に薄まると自動着手されてしまう）。
    merged["human_reason"] = next(
        (idea["human_reason"] for idea in ideas if idea["human_reason"] != inbox.HOLD_NONE),
        inbox.HOLD_NONE,
    )

    first_position = idea_positions[0]
    result = []
    for i, item in enumerate(items):
        if item["kind"] != inbox.KIND_IDEA:
            result.append(item)
        elif i == first_position:
            result.append(merged)
    return result


def _rescue_index(items: list[dict]) -> int:
    """全アイテムが「対象外」なら先頭の位置(0)、そうでなければ -1 を返す。

    1本のメモを割った結果が予定/資料だけになると、そのメモは「要確認」にも
    載らず、未処理置き場からも移動済みなので誰の目にも触れず消える。
    握りつぶし防止に、全滅したときだけ先頭1件を「要確認」へ繰り上げる
    （inbox.build_properties の rescue_excluded）。1件でも「アイデア」が
    混じっていれば、それが表に出るので繰り上げない。
    プロンプトも分類ロジックも変えず、この後処理だけで拾う方針
    （2026-09-09、本人の判断。「プロンプトも今のままでいい」「全部対象外なら
    機械的に最初の要素だけ要確認にすればいい」）。
    """
    statuses = [inbox.resolve_status(item) for item in items]
    return 0 if statuses and all(s == inbox.STATUS_EXCLUDED for s in statuses) else -1


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

    items = _merge_ideas(items)

    if config.dry_run:
        log.log(f"[{label}] （dry-run）{len(items)}件に分割されました:")
        for item in items:
            log.log(f"    - [{item['kind']}] {item['title']}")
        return 0

    # 同じ生ログ由来の全アイテムをまとめて1回で判定する。
    # 1件ずつ判定すると、同じ話題でも別ジャンルに割れる不具合があったため
    # （2026-09-05、本人の指摘）。
    item_genres = genre.assign_for_capture_batch(gemini, items)

    # 分割元メモの識別子。同じ生ログから割れた兄弟は全て同じ文字列を持つ。
    # done を打ったとき、この一致で兄弟を辿ってまとめて完了にする（croco_cli.py）。
    origin = f"{source['title']} ({page_id})".strip()

    # 1本のメモを割った結果が予定/資料だけ（全て「対象外」）だと、そのメモは
    # 要確認にも載らず消える。全滅したときだけ先頭1件を「要確認」へ繰り上げる。
    rescue_index = _rescue_index(items)

    # 1件でも登録に失敗したら移動しない。
    # 移動しなければ次回まるごと再試行されるだけで済む。
    registered = 0
    for idx, (item, item_genre) in enumerate(zip(items, item_genres)):
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
                # 既存アイテムへの統合では新規ページを作らないので、由来メモも
                # 繰り上げも付かない。予定の重複でのみ通る稀な経路なので初版は許容。
                dedupe.merge_into(
                    client, duplicate, new_title=item["title"], new_body=item["body"]
                )
                log.log(
                    f"[{label}] 「{item['title']}」は既存の「{duplicate.title}」"
                    f"({duplicate.id}) と重複と判定し統合しました。"
                )
            else:
                client.create_page(
                    data_source_id=data_source_id,
                    properties=inbox.build_properties(
                        item,
                        spoken_at=captured_at,
                        genre=item_genre,
                        origin=origin,
                        rescue_excluded=(idx == rescue_index),
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
