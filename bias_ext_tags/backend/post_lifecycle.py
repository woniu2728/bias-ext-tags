from __future__ import annotations

from bias_ext_tags.backend.services import TagService
from bias_ext_tags.backend.tag_relationships import get_discussion_tag_ids


def apply_post_created(*, post, context: dict | None = None, **kwargs) -> dict:
    if not (context or {}).get("is_approved"):
        return {}

    discussion_id = getattr(post, "discussion_id", None)
    if not discussion_id:
        return {}
    discussion = post.discussion.__class__.objects.get(id=discussion_id)

    tag_ids = get_discussion_tag_ids(discussion)
    if not tag_ids:
        return {}

    TagService.update_tag_latest_discussion(discussion, tag_ids)
    return {
        "discussion_id": discussion.id,
        "affected_tag_ids": tag_ids,
    }


def apply_post_hidden(*, post, context: dict | None = None, **kwargs) -> dict:
    resolved_context = context or {}
    if not resolved_context.get("was_counted"):
        return {}

    discussion_id = getattr(post, "discussion_id", None)
    if not discussion_id:
        return {}
    discussion = post.discussion.__class__.objects.get(id=discussion_id)

    tag_ids = get_discussion_tag_ids(discussion)
    if not tag_ids:
        return {}

    if resolved_context.get("is_hidden"):
        latest_tag_ids = TagService.tag_ids_where_discussion_is_latest(discussion, tag_ids)
        if latest_tag_ids:
            TagService.refresh_tag_stats(latest_tag_ids)
            affected_tag_ids = latest_tag_ids
        else:
            affected_tag_ids = ()
    else:
        TagService.update_tag_latest_discussion(discussion, tag_ids)
        affected_tag_ids = tag_ids

    return {
        "discussion_id": discussion.id,
        "affected_tag_ids": tuple(affected_tag_ids),
    }
