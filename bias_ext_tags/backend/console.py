from bias_ext_tags.backend.services import TagService


def refresh_tag_stats_console_command(options: dict | None = None) -> dict:
    tag_ids = (options or {}).get("tag_ids")
    if tag_ids is None:
        tag_ids = (options or {}).get("tag_id")
    if tag_ids is None:
        normalized_tag_ids = None
    elif isinstance(tag_ids, (list, tuple, set)):
        normalized_tag_ids = [int(item) for item in tag_ids if item is not None]
    else:
        normalized_tag_ids = [int(tag_ids)]

    TagService.refresh_tag_stats(normalized_tag_ids)
    refreshed_count = None if normalized_tag_ids is None else len(set(normalized_tag_ids))
    return {
        "status": "ok",
        "message": "已刷新全部标签统计" if refreshed_count is None else f"已刷新 {refreshed_count} 个标签统计",
        "details": {
            "tag_ids": normalized_tag_ids or [],
            "refreshed_count": refreshed_count,
        },
    }
