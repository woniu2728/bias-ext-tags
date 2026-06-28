from __future__ import annotations


def tag_service_provider() -> dict:
    from bias_ext_tags.backend.models import DiscussionTag, Tag, TagState
    from bias_ext_tags.backend.services import TagService

    return {
        "model": Tag,
        "state_model": TagState,
        "relationship_model": DiscussionTag,
        "summaries_by_slugs": _summaries_by_slugs,
        "get_scope_label": TagService.get_scope_label,
        "validate_parent_assignment": TagService.validate_parent_assignment,
        "validate_scope_configuration": TagService.validate_scope_configuration,
        "create_tag": TagService.create_tag,
        "update_tag": TagService.update_tag,
        "move_tag": TagService.move_tag,
        "order_tags": TagService.order_tags,
        "delete_tag": TagService.delete_tag,
        "dispatch_refresh_tag_stats": TagService.dispatch_refresh_tag_stats,
        "filter_tags_for_user": TagService.filter_tags_for_user,
        "can_view_tag": TagService.can_view_tag,
        "can_start_discussion_in_tag": TagService.can_start_discussion_in_tag,
        "can_add_to_discussion": TagService.can_add_to_discussion,
        "can_reply_in_tag": TagService.can_reply_in_tag,
        "state_for_user": TagService.state_for_user,
        "prefetch_state_for_user": TagService.prefetch_state_for_user,
        "mark_tag_read": TagService.mark_tag_read,
        "refresh_discussion_tag_stats": TagService.refresh_discussion_tag_stats,
        "refresh_tag_stats": TagService.refresh_tag_stats,
        "ensure_can_start_discussion": TagService.ensure_can_start_discussion,
        "ensure_can_change_discussion_tags": TagService.ensure_can_change_discussion_tags,
    }


def _summaries_by_slugs(slugs) -> dict[str, dict]:
    from bias_ext_tags.backend.models import Tag

    normalized_slugs = sorted({
        str(slug or "").strip()
        for slug in slugs or ()
        if str(slug or "").strip()
    })
    if not normalized_slugs:
        return {}

    return {
        item["slug"]: item
        for item in Tag.objects.filter(slug__in=normalized_slugs).values("id", "name", "slug")
    }


