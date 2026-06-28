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
