from bias_core.extensions import ExtensionEventListenerDefinition
from bias_core.extensions.runtime import (
    create_runtime_timeline_from_builder,
    dispatch_runtime_tag_stats_refresh,
    refresh_runtime_discussion_tag_stats,
    refresh_runtime_tag_stats,
)
from bias_ext_tags.backend.events import (
    DiscussionTaggedEvent,
    DiscussionTagStatsRefreshEvent,
    TagStatsRefreshRequestedEvent,
)


def tag_event_listener_definitions():
    return (
        ExtensionEventListenerDefinition(
            event_type="discussions.discussion.approved",
            handler=handle_discussion_approved_tag_stats,
            description="讨论审核通过后刷新关联标签统计。",
        ),
        ExtensionEventListenerDefinition(
            event_type=DiscussionTagStatsRefreshEvent,
            handler=handle_discussion_tag_stats_refresh,
            description="刷新讨论关联标签统计。",
        ),
        ExtensionEventListenerDefinition(
            event_type=TagStatsRefreshRequestedEvent,
            handler=handle_tag_stats_refresh_requested,
            description="调度标签统计刷新任务。",
        ),
    )


def post_event_listener_definitions():
    return (
        ExtensionEventListenerDefinition(
            event_type=DiscussionTaggedEvent,
            handler=handle_discussion_tagged,
            description="刷新标签统计并写入标签变更事件帖。",
        ),
        ExtensionEventListenerDefinition(
            event_type="posts.post.created",
            handler=handle_post_created_tag_stats,
            description="回复发布后刷新讨论关联标签统计。",
        ),
        ExtensionEventListenerDefinition(
            event_type="posts.post.approved",
            handler=handle_post_approved_tag_stats,
            description="回复审核通过后刷新讨论关联标签统计。",
        ),
        ExtensionEventListenerDefinition(
            event_type="posts.post.deleted",
            handler=handle_post_deleted_tag_stats,
            description="回复删除后刷新讨论关联标签统计。",
        ),
        ExtensionEventListenerDefinition(
            event_type="posts.post.hidden",
            handler=handle_post_hidden_tag_stats,
            description="回复隐藏状态变更后刷新讨论关联标签统计。",
        ),
        ExtensionEventListenerDefinition(
            event_type="posts.post.rejected",
            handler=handle_post_rejected_tag_stats,
            description="回复审核拒绝后刷新讨论关联标签统计。",
        ),
    )


def enrich_realtime_tags_included_payload(*, discussion=None, post_payload=None, extension_context=None, payload=None):
    tags = {}
    if discussion is not None:
        for tag in _iter_discussion_tags(discussion):
            _merge_tag_payload(tags, tag, fallback_discussion=discussion)
    else:
        tags_context = dict((extension_context or {}).get("tags") or {})
        tag_ids = tags_context.get("tag_ids") or []
        if not tag_ids:
            return {}
        from bias_ext_tags.backend.models import Tag

        for tag in Tag.objects.select_related("last_posted_discussion").filter(id__in=tag_ids):
            _merge_tag_payload(tags, tag)
    if not tags:
        return {}
    return {"tags": list(tags.values())}


def _iter_discussion_tags(discussion):
    from bias_ext_tags.backend.tag_relationships import get_discussion_tags

    return get_discussion_tags(discussion)


def _merge_tag_payload(target: dict, tag, *, fallback_discussion=None) -> None:
    from bias_ext_tags.backend.handlers import _serialize_tag

    payload = _serialize_tag(tag, user=None, include_children=False)
    if not payload or payload.get("id") is None:
        return
    if fallback_discussion is not None:
        payload["last_posted_discussion"] = {
            "id": fallback_discussion.id,
            "title": fallback_discussion.title,
            "slug": fallback_discussion.slug,
            "last_post_number": fallback_discussion.last_post_number,
            "last_posted_at": fallback_discussion.last_posted_at,
        }
    target[int(payload["id"])] = payload


def handle_discussion_approved_tag_stats(event) -> None:
    refresh_runtime_discussion_tag_stats(event.discussion_id)


def handle_discussion_tagged(event: DiscussionTaggedEvent) -> None:
    if event.tag_ids:
        refresh_runtime_tag_stats(list(event.tag_ids))
    else:
        refresh_runtime_discussion_tag_stats(event.discussion_id)
    create_runtime_timeline_from_builder(
        event,
        "discussion_tagged",
        extra={"post_type": "discussionTagged"},
    )


def handle_post_created_tag_stats(event) -> None:
    return None


def handle_post_approved_tag_stats(event) -> None:
    refresh_runtime_discussion_tag_stats(event.discussion_id)


def handle_post_deleted_tag_stats(event) -> None:
    refresh_runtime_discussion_tag_stats(event.discussion_id)


def handle_post_hidden_tag_stats(event) -> None:
    refresh_runtime_discussion_tag_stats(event.discussion_id)


def handle_post_rejected_tag_stats(event) -> None:
    refresh_runtime_discussion_tag_stats(event.discussion_id)


def handle_discussion_tag_stats_refresh(event: DiscussionTagStatsRefreshEvent) -> None:
    refresh_runtime_discussion_tag_stats(event.discussion_id)


def handle_tag_stats_refresh_requested(event: TagStatsRefreshRequestedEvent) -> None:
    if not event.tag_ids:
        return

    dispatch_runtime_tag_stats_refresh(list(event.tag_ids))
