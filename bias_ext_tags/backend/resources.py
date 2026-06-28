from __future__ import annotations

from bias_core.extensions import (
    ResourceDefinition,
    ResourceEndpointDefinition,
    ResourceFieldDefinition,
    ResourceRelationshipDefinition,
)
from bias_ext_tags.backend.constants import EXTENSION_ID
from bias_ext_tags.backend.handlers import (
    dispatch_tag_create,
    dispatch_tag_delete,
    dispatch_tag_index,
    dispatch_tag_popular,
    dispatch_tag_show,
    dispatch_tag_show_by_slug,
    dispatch_tag_update,
)
from bias_ext_tags.backend.models import Tag
from bias_core.extensions.runtime import (
    can_runtime_add_to_discussion,
    can_runtime_reply_in_tag,
    can_runtime_start_discussion_in_tag,
    filter_runtime_tags_for_user,
    has_runtime_forum_permission,
)


def tag_resource_definition():
    return ResourceDefinition(
        resource="tag",
        module_id=EXTENSION_ID,
        resolver=serialize_tag_base,
        description="论坛标签主资源。",
    )


def tag_resource_definitions():
    return (tag_resource_definition(),)


def discussion_resource_field_definitions():
    return (
        ResourceFieldDefinition(
            resource="discussion",
            field="tags",
            module_id=EXTENSION_ID,
            resolver=resolve_discussion_tags,
            description="讨论关联的标签列表。",
            prefetch_related=("discussion_tags__tag",),
        ),
    )


def discussion_resource_relationship_definitions():
    from bias_ext_tags.backend.discussion_relationships import set_discussion_tags_relationship

    return (
        ResourceRelationshipDefinition(
            resource="discussion",
            relationship="tags",
            module_id=EXTENSION_ID,
            resolver=resolve_discussion_tags,
            description="讨论关联标签关系。",
            prefetch_related=("discussion_tags__tag",),
            resource_type="tag",
            many=True,
            writable=True,
            value_type="array",
            setter=set_discussion_tags_relationship,
        ),
    )


def forum_resource_field_definitions():
    return (
        ResourceFieldDefinition(
            resource="forum",
            field="tags",
            module_id=EXTENSION_ID,
            resolver=resolve_forum_tags,
            description="论坛首页可见标签树。",
            prefetch_related=("children",),
        ),
        ResourceFieldDefinition(
            resource="forum",
            field="can_bypass_tag_counts",
            module_id=EXTENSION_ID,
            resolver=resolve_forum_can_bypass_tag_counts,
            description="当前用户是否可绕过发帖标签数量限制。",
        ),
    )


def forum_resource_relationship_definitions():
    return (
        ResourceRelationshipDefinition(
            resource="forum",
            relationship="tags",
            module_id=EXTENSION_ID,
            resolver=resolve_forum_tags,
            description="论坛首页可见标签关系。",
            resource_type="tag",
            many=True,
        ),
    )


def tag_resource_field_definitions():
    return (
        ResourceFieldDefinition(
            resource="tag",
            field="can_start_discussion",
            module_id=EXTENSION_ID,
            resolver=resolve_tag_can_start_discussion,
            description="当前用户是否可以在该标签下发起讨论。",
        ),
        ResourceFieldDefinition(
            resource="tag",
            field="can_add_to_discussion",
            module_id=EXTENSION_ID,
            resolver=resolve_tag_can_add_to_discussion,
            description="当前用户是否可以把该标签添加到已有讨论。",
        ),
        ResourceFieldDefinition(
            resource="tag",
            field="can_reply",
            module_id=EXTENSION_ID,
            resolver=resolve_tag_can_reply,
            description="当前用户是否可以在该标签下回复。",
        ),
        ResourceFieldDefinition(
            resource="tag",
            field="last_posted_discussion",
            module_id=EXTENSION_ID,
            resolver=resolve_tag_last_posted_discussion,
            description="标签下最后活跃讨论摘要。",
            select_related=("last_posted_discussion",),
        ),
        ResourceFieldDefinition(
            resource="tag",
            field="state",
            module_id=EXTENSION_ID,
            resolver=resolve_tag_state,
            description="当前用户在该标签上的阅读与隐藏状态。",
            prefetch_related=("user_states",),
        ),
    )


def tag_resource_relationship_definitions():
    return (
        ResourceRelationshipDefinition(
            resource="tag",
            relationship="last_posted_discussion",
            module_id=EXTENSION_ID,
            resolver=resolve_tag_last_posted_discussion,
            description="标签下最后活跃讨论摘要。",
            select_related=("last_posted_discussion",),
        ),
    )


def tag_resource_endpoints():
    return (
        ResourceEndpointDefinition(
            resource="tag",
            endpoint="create",
            module_id=EXTENSION_ID,
            handler=dispatch_tag_create,
            methods=("POST",),
            path="/tags",
            absolute_path=True,
            auth_required=True,
        ),
        ResourceEndpointDefinition(
            resource="tag",
            endpoint="index",
            module_id=EXTENSION_ID,
            handler=dispatch_tag_index,
            methods=("GET",),
            path="/tags",
            absolute_path=True,
        ),
        ResourceEndpointDefinition(
            resource="tag",
            endpoint="popular",
            module_id=EXTENSION_ID,
            handler=dispatch_tag_popular,
            methods=("GET",),
            path="/tags/popular",
            absolute_path=True,
        ),
        ResourceEndpointDefinition(
            resource="tag",
            endpoint="show",
            module_id=EXTENSION_ID,
            handler=dispatch_tag_show,
            methods=("GET",),
            path="/tags/{object_id}",
            absolute_path=True,
        ),
        ResourceEndpointDefinition(
            resource="tag",
            endpoint="show-by-slug",
            module_id=EXTENSION_ID,
            handler=dispatch_tag_show_by_slug,
            methods=("GET",),
            path="/tags/slug/{object_id}",
            absolute_path=True,
        ),
        ResourceEndpointDefinition(
            resource="tag",
            endpoint="update",
            module_id=EXTENSION_ID,
            handler=dispatch_tag_update,
            methods=("PATCH",),
            path="/tags/{object_id}",
            absolute_path=True,
            auth_required=True,
        ),
        ResourceEndpointDefinition(
            resource="tag",
            endpoint="delete",
            module_id=EXTENSION_ID,
            handler=dispatch_tag_delete,
            methods=("DELETE",),
            path="/tags/{object_id}",
            absolute_path=True,
            auth_required=True,
        ),
    )


def serialize_tag_base(tag, context: dict) -> dict:
    payload = {
        "id": tag.id,
        "name": tag.name,
        "slug": tag.slug,
        "description": tag.description,
        "color": tag.color,
        "icon": tag.icon,
        "background_url": tag.background_url,
        "position": tag.position,
        "parent_id": tag.parent_id,
        "is_hidden": tag.is_hidden,
        "is_restricted": tag.is_restricted,
        "view_scope": tag.view_scope,
        "start_discussion_scope": tag.start_discussion_scope,
        "reply_scope": tag.reply_scope,
        "discussion_count": tag.discussion_count,
        "last_posted_at": tag.last_posted_at,
        "created_at": tag.created_at,
        "updated_at": tag.updated_at,
    }
    state = resolve_tag_state(tag, context)
    if state is not None:
        payload["state"] = state
    return payload


def resolve_tag_state(tag, context: dict) -> dict | None:
    from bias_ext_tags.backend.services import TagService

    state = TagService.state_for_user(tag, context.get("user"))
    if state is None:
        return None
    return {
        "marked_as_read_at": state.marked_as_read_at,
        "is_hidden": bool(state.is_hidden),
    }


def resolve_discussion_tags(discussion, context: dict) -> list[dict]:
    from bias_ext_tags.backend.tag_relationships import serialize_discussion_tag_summaries

    return serialize_discussion_tag_summaries(discussion)


def resolve_forum_tags(forum, context: dict) -> list[dict]:
    from django.db.models import Prefetch
    from bias_ext_tags.backend.services import TagService

    user = context.get("user")
    child_queryset = filter_runtime_tags_for_user(
        Tag.objects.filter(is_hidden=False).order_by("position", "name"),
        user,
        action="view",
    )
    child_queryset = TagService.prefetch_state_for_user(child_queryset, user)
    queryset = filter_runtime_tags_for_user(
        Tag.objects.filter(parent__isnull=True, is_hidden=False)
        .select_related("last_posted_discussion")
        .prefetch_related(Prefetch("children", queryset=child_queryset, to_attr="visible_children"))
        .order_by("position", "name"),
        user,
        action="view",
    )
    queryset = TagService.prefetch_state_for_user(queryset, user)
    return [_serialize_forum_tag(tag, context) for tag in queryset]


def resolve_forum_can_bypass_tag_counts(forum, context: dict) -> bool:
    user = context.get("user")
    return has_runtime_forum_permission(user, "bypassTagCounts")


def resolve_discussion_tagged_event_data(post, context: dict) -> dict | None:
    added = []
    removed = []
    for line in _normalized_lines(getattr(post, "content", "")):
        if line.startswith("added:"):
            added = [item for item in line.removeprefix("added:").split("|") if item]
        elif line.startswith("removed:"):
            removed = [item for item in line.removeprefix("removed:").split("|") if item]

    return {
        "kind": "discussionTagged",
        "added_tags": added,
        "removed_tags": removed,
    }


def resolve_post_event_mentions_tags(post, context: dict | None = None) -> list[Tag]:
    if getattr(post, "type", "") != "discussionTagged":
        return []
    event_data = resolve_discussion_tagged_event_data(post, context or {}) or {}
    slugs = []
    for slug in [*event_data.get("added_tags", []), *event_data.get("removed_tags", [])]:
        if slug and slug not in slugs:
            slugs.append(slug)
    if not slugs:
        return []

    tags_by_slug = {
        tag.slug: tag
        for tag in Tag.objects.filter(slug__in=slugs).select_related("last_posted_discussion")
    }
    return [tags_by_slug[slug] for slug in slugs if slug in tags_by_slug]


def resolve_tag_can_start_discussion(tag, context: dict) -> bool:
    user = context.get("user")
    return can_runtime_start_discussion_in_tag(tag, user)


def resolve_tag_can_add_to_discussion(tag, context: dict) -> bool:
    user = context.get("user")
    return can_runtime_add_to_discussion(tag, user)


def resolve_tag_can_reply(tag, context: dict) -> bool:
    user = context.get("user")
    return can_runtime_reply_in_tag(tag, user)


def resolve_tag_last_posted_discussion(tag, context: dict) -> dict | None:
    discussion = getattr(tag, "last_posted_discussion", None)
    if not discussion:
        return None

    return {
        "id": discussion.id,
        "title": discussion.title,
        "slug": discussion.slug,
        "last_post_number": discussion.last_post_number,
        "last_posted_at": discussion.last_posted_at,
    }


def _normalized_lines(content: str | None) -> list[str]:
    return [
        line.strip()
        for line in (content or "").splitlines()
        if line.strip()
    ]


def _serialize_forum_tag(tag, context: dict) -> dict:
    payload = serialize_tag_base(tag, context)
    payload["can_start_discussion"] = resolve_tag_can_start_discussion(tag, context)
    payload["can_add_to_discussion"] = resolve_tag_can_add_to_discussion(tag, context)
    payload["can_reply"] = resolve_tag_can_reply(tag, context)
    payload["last_posted_discussion"] = resolve_tag_last_posted_discussion(tag, context)
    payload["children"] = [
        _serialize_forum_tag(child, context)
        for child in getattr(tag, "visible_children", [])
    ]
    return payload

