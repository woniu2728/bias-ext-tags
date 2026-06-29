from __future__ import annotations

from bias_core.extensions.platform import dispatch_forum_event_after_commit
from bias_core.extensions.runtime import (
    ensure_can_start_discussion_in_runtime_tags,
)
from bias_ext_tags.backend.events import DiscussionTaggedEvent
from bias_ext_tags.backend.services import TagService
from bias_ext_tags.backend.tag_relationships import (
    get_discussion_tag_ids,
    get_discussion_tag_ids_for_stats,
    get_discussion_tag_links,
    get_discussion_tag_names,
    get_discussion_tags,
    replace_discussion_tags,
    serialize_discussion_tag_summaries,
    tag_has_discussions,
)


def set_discussion_tags_relationship(discussion, value, context: dict | None = None) -> None:
    context = context or {}
    user = context.get("user")
    tag_ids = _relationship_tag_ids(value)
    if context.get("creating"):
        tags = tuple(ensure_can_start_discussion_in_runtime_tags(user, tag_ids))
        previous_links = ()
    else:
        previous_links = tuple(get_discussion_tag_links(discussion))
        existing_tag_ids = tuple(
            link.tag_id
            for link in previous_links
            if getattr(link, "tag_id", None) is not None
        )
        tags = tuple(TagService.ensure_can_change_discussion_tags(
            user,
            discussion,
            tag_ids,
            existing_tag_ids=existing_tag_ids,
        ))

    result = replace_discussion_tags(discussion, tags, previous_links=previous_links)
    affected_tag_ids = tuple(result["affected_tag_ids"])
    context["tags_relationship_result"] = result

    if not context.get("creating"):
        added_tags = tuple(result["added_tags"])
        removed_tags = tuple(result["removed_tags"])
        if added_tags or removed_tags:
            dispatch_forum_event_after_commit(
                DiscussionTaggedEvent(
                    discussion_id=discussion.id,
                    actor_user_id=context.get("actor_user_id"),
                    added_tags=added_tags,
                    removed_tags=removed_tags,
                    tag_ids=affected_tag_ids,
                )
            )


def _relationship_tag_ids(value) -> list[int]:
    if value is None:
        return []
    if isinstance(value, dict) and "data" in value:
        value = value["data"]
    if not isinstance(value, (list, tuple)):
        value = [value]

    tag_ids: list[int] = []
    for item in value:
        raw_id = item.get("id") if isinstance(item, dict) else item
        try:
            tag_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        tag_ids.append(tag_id)
    return tag_ids


__all__ = [
    "get_discussion_tag_ids",
    "get_discussion_tag_ids_for_stats",
    "get_discussion_tag_links",
    "get_discussion_tag_names",
    "get_discussion_tags",
    "replace_discussion_tags",
    "serialize_discussion_tag_summaries",
    "set_discussion_tags_relationship",
    "tag_has_discussions",
]
