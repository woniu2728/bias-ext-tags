from bias_core.extensions import ExtensionEventListenerDefinition
from bias_core.extensions.platform import log_admin_action
from bias_ext_tags.backend.events import (
    DiscussionTaggedEvent,
    DiscussionTagStatsRefreshEvent,
    TagStatsRefreshRequestedEvent,
)


def create_runtime_timeline_from_builder(*args, **kwargs):
    from bias_core.extensions.runtime import create_runtime_timeline_from_builder as runtime_create_timeline_from_builder

    return runtime_create_timeline_from_builder(*args, **kwargs)


def refresh_runtime_discussion_tag_stats(*args, **kwargs):
    from bias_core.extensions.runtime import refresh_runtime_discussion_tag_stats as runtime_refresh_discussion_tag_stats

    return runtime_refresh_discussion_tag_stats(*args, **kwargs)


def refresh_runtime_tag_stats(*args, **kwargs):
    from bias_core.extensions.runtime import refresh_runtime_tag_stats as runtime_refresh_tag_stats

    return runtime_refresh_tag_stats(*args, **kwargs)


def dispatch_runtime_tag_stats_refresh(*args, **kwargs):
    from bias_core.extensions.runtime import dispatch_runtime_tag_stats_refresh as runtime_dispatch_tag_stats_refresh

    return runtime_dispatch_tag_stats_refresh(*args, **kwargs)


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

        for tag in Tag.objects.select_related("last_posted_discussion", "last_posted_user").filter(id__in=tag_ids):
            _merge_tag_payload(tags, tag)
    if not tags:
        return {}
    return {"tags": list(tags.values())}


def _iter_discussion_tags(discussion):
    from bias_ext_tags.backend.tag_relationships import get_discussion_tags

    return get_discussion_tags(discussion)


def _merge_tag_payload(target: dict, tag, *, fallback_discussion=None) -> None:
    from bias_ext_tags.backend.responses import serialize_tag

    payload = serialize_tag(tag, user=None, include_children=False)
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
    return None


def handle_discussion_tagged(event: DiscussionTaggedEvent) -> None:
    log_discussion_tagged_audit(event)
    create_runtime_timeline_from_builder(
        event,
        "discussion_tagged",
        extra={"post_type": "discussionTagged"},
        merge_strategy="same_actor_reversible",
    )


def log_discussion_tagged_audit(event: DiscussionTaggedEvent) -> None:
    from django.contrib.auth import get_user_model
    from bias_ext_tags.backend.models import Tag

    actor = None
    actor_user_id = getattr(event, "actor_user_id", None)
    if actor_user_id:
        actor = get_user_model().objects.filter(id=actor_user_id).first()

    current_slugs = tuple(
        Tag.objects.filter(discussion_tags__discussion_id=event.discussion_id)
        .order_by("slug")
        .values_list("slug", flat=True)
    )
    added_slugs = tuple(
        Tag.objects.filter(id__in=event.added_tag_ids)
        .order_by("slug")
        .values_list("slug", flat=True)
    )
    removed_slugs = tuple(
        Tag.objects.filter(id__in=event.removed_tag_ids)
        .order_by("slug")
        .values_list("slug", flat=True)
    )
    previous_slugs = tuple(
        sorted((set(current_slugs) - set(added_slugs)) | set(removed_slugs))
    )

    log_admin_action(
        actor,
        "discussion.tagged",
        target_type="discussion",
        target_id=event.discussion_id,
        data={
            "discussion_id": event.discussion_id,
            "old_tags": list(previous_slugs),
            "new_tags": list(current_slugs),
            "added_tags": list(added_slugs),
            "removed_tags": list(removed_slugs),
        },
    )


def handle_post_created_tag_stats(event) -> None:
    return None


def handle_post_approved_tag_stats(event) -> None:
    return None


def handle_post_deleted_tag_stats(event) -> None:
    return None


def handle_post_hidden_tag_stats(event) -> None:
    return None


def handle_post_rejected_tag_stats(event) -> None:
    return None


def handle_discussion_tag_stats_refresh(event: DiscussionTagStatsRefreshEvent) -> None:
    refresh_runtime_discussion_tag_stats(event.discussion_id)


def handle_tag_stats_refresh_requested(event: TagStatsRefreshRequestedEvent) -> None:
    if not event.tag_ids:
        return

    dispatch_runtime_tag_stats_refresh(list(event.tag_ids))
