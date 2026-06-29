from __future__ import annotations

from bias_core.extensions import (
    ResourceFieldDefinition,
    ResourceRelationshipDefinition,
)
from bias_ext_tags.backend.constants import EXTENSION_ID
from bias_ext_tags.backend.models import Tag
from bias_ext_tags.backend.tag_resource import TagResource


def tag_resource_definition():
    return TagResource()


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
            prefetch_related=("discussion_tags__tag", "discussion_tags__tag__parent"),
        ),
        ResourceFieldDefinition(
            resource="discussion",
            field="can_tag",
            module_id=EXTENSION_ID,
            resolver=resolve_discussion_can_tag,
            description="当前用户是否可以调整讨论标签。",
        ),
    )


def discussion_resource_relationship_definitions():
    from bias_ext_tags.backend.discussion_relationships import set_discussion_tags_relationship

    return (
        ResourceRelationshipDefinition(
            resource="discussion",
            relationship="tags",
            module_id=EXTENSION_ID,
            resolver=resolve_discussion_tag_resources,
            description="讨论关联标签关系。",
            prefetch_related=("discussion_tags__tag", "discussion_tags__tag__parent"),
            resource_type="tag",
            many=True,
            writable=True,
            value_type="array",
            required_on_create=discussion_tags_required_on_create,
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
            resolver=resolve_tag_last_posted_discussion_resource,
            description="标签下最后活跃讨论资源。",
            select_related=("last_posted_discussion",),
            resource_type="discussion",
        ),
    )


def serialize_tag_base(tag, context: dict) -> dict:
    from bias_ext_tags.backend.services import TagService

    payload = {
        "id": tag.id,
        "name": tag.name,
        "slug": resolve_tag_slug(tag, context),
        "description": tag.description,
        "color": tag.color,
        "icon": tag.icon,
        "background_url": tag.background_url,
        "position": tag.position,
        "default_sort": tag.default_sort,
        "parent_id": tag.parent_id,
        "is_hidden": tag.is_hidden,
        "is_primary": TagService.is_primary_tree_tag(tag),
        "is_child": TagService.is_child_tag(tag),
        "discussion_count": tag.discussion_count,
        "last_posted_at": tag.last_posted_at,
        "created_at": tag.created_at,
        "updated_at": tag.updated_at,
    }
    payload.update({
        "defaultSort": payload["default_sort"],
        "isHidden": payload["is_hidden"],
        "isPrimary": payload["is_primary"],
        "isChild": payload["is_child"],
        "discussionCount": payload["discussion_count"],
        "lastPostedAt": payload["last_posted_at"],
    })
    if can_view_tag_stored_slug(tag, context):
        payload["stored_slug"] = tag.slug
        payload["storedSlug"] = tag.slug
    if can_view_tag_admin_fields(context):
        payload.update({
            "is_restricted": tag.is_restricted,
            "isRestricted": tag.is_restricted,
            "view_scope": tag.view_scope,
            "start_discussion_scope": tag.start_discussion_scope,
            "reply_scope": tag.reply_scope,
        })
    state = resolve_tag_state(tag, context)
    if state is not None:
        payload["state"] = state
    return payload


def resolve_tag_slug(tag, context: dict) -> str:
    from bias_ext_tags.backend.services import TagService

    return TagService.to_tag_slug(tag)


def can_view_tag_admin_fields(context: dict) -> bool:
    user = context.get("user")
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
    )


def can_view_tag_stored_slug(tag, context: dict) -> bool:
    from bias_core.extensions.runtime import has_runtime_forum_permission

    user = context.get("user")
    return bool(user and getattr(user, "is_authenticated", False) and has_runtime_forum_permission(user, "tag.edit"))


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


def resolve_discussion_tag_resources(discussion, context: dict) -> list[Tag]:
    from bias_ext_tags.backend.tag_relationships import get_discussion_tags

    return get_discussion_tags(discussion)


def resolve_discussion_can_tag(discussion, context: dict) -> bool:
    from bias_ext_tags.backend.services import TagService

    return TagService.can_tag_discussion(discussion, context.get("user"))


def discussion_tags_required_on_create(discussion, context: dict) -> bool:
    user = context.get("user")
    if not user or not getattr(user, "is_authenticated", False):
        return True

    from bias_core.extensions.runtime import has_runtime_forum_permission

    return not has_runtime_forum_permission(user, "bypassTagCounts")


def resolve_forum_tags(forum, context: dict) -> list[dict]:
    from bias_core.extensions.runtime import filter_runtime_tags_for_user
    from bias_ext_tags.backend.services import TagService

    user = context.get("user")
    primary_queryset = filter_runtime_tags_for_user(
        Tag.objects.filter(parent__isnull=True, position__isnull=False, is_hidden=False)
        .select_related("last_posted_discussion", "parent")
        .order_by(*TagService.structure_order_by()),
        user,
        action="view",
    )
    primary_queryset = TagService.prefetch_state_for_user(primary_queryset, user)
    primary_tags = list(primary_queryset)

    secondary_queryset = filter_runtime_tags_for_user(
        Tag.objects.filter(parent__isnull=True, position__isnull=True, is_hidden=False)
        .select_related("last_posted_discussion", "parent")
        .order_by("-discussion_count", "name"),
        user,
        action="view",
    )
    secondary_queryset = TagService.prefetch_state_for_user(secondary_queryset, user)
    secondary_tags = list(secondary_queryset[:4])

    return [_serialize_forum_tag(tag, context, include_children=False) for tag in [*primary_tags, *secondary_tags]]


def resolve_forum_can_bypass_tag_counts(forum, context: dict) -> bool:
    from bias_core.extensions.runtime import has_runtime_forum_permission

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
    from bias_core.extensions.runtime import can_runtime_start_discussion_in_tag

    return _cached_tag_permission(context, tag, "can_start_discussion", can_runtime_start_discussion_in_tag)


def resolve_tag_can_add_to_discussion(tag, context: dict) -> bool:
    from bias_core.extensions.runtime import can_runtime_add_to_discussion

    return _cached_tag_permission(context, tag, "can_add_to_discussion", can_runtime_add_to_discussion)


def resolve_tag_can_reply(tag, context: dict) -> bool:
    from bias_core.extensions.runtime import can_runtime_reply_in_tag

    return _cached_tag_permission(context, tag, "can_reply", can_runtime_reply_in_tag)


def _cached_tag_permission(context: dict, tag, permission: str, resolver) -> bool:
    user = context.get("user")
    cache = context.setdefault("_tag_permission_results", {})
    key = (getattr(tag, "id", None), getattr(user, "id", None), context.get("action", "view"), permission)
    if key not in cache:
        cache[key] = bool(resolver(tag, user))
    return cache[key]


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


def resolve_tag_last_posted_discussion_resource(tag, context: dict):
    return getattr(tag, "last_posted_discussion", None)


def resolve_tag_parent(tag, context: dict):
    parent = getattr(tag, "parent", None)
    if parent is None:
        return None
    from bias_ext_tags.backend.services import TagService

    if not TagService.can_view_tag(parent, context.get("user")):
        return None
    return parent


def tag_parent_relationship_writable(tag, context: dict) -> bool:
    attributes = _tag_resource_attributes(context)
    return bool(attributes.get("is_primary") or attributes.get("isPrimary"))


def set_tag_parent_relationship(tag, value, context: dict) -> None:
    tag.parent_id = _tag_relationship_resource_id(value)


def _tag_resource_attributes(context: dict) -> dict:
    payload = context.get("payload") if isinstance(context, dict) else None
    data = payload.get("data") if isinstance(payload, dict) else None
    attributes = data.get("attributes") if isinstance(data, dict) else None
    return dict(attributes) if isinstance(attributes, dict) else {}


def _tag_relationship_resource_id(value) -> int | None:
    if isinstance(value, dict) and "data" in value:
        value = value.get("data")
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        value = value.get("id")
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def resolve_tag_children(tag, context: dict) -> list[Tag]:
    children = getattr(tag, "visible_children", None)
    if children is None:
        from bias_ext_tags.backend.services import TagService

        children = getattr(tag, "children", []).all().order_by(*TagService.child_order_by())
    forbidden_tag_ids = context.get("forbidden_tag_ids")
    if forbidden_tag_ids is None:
        from bias_ext_tags.backend.services import TagService

        forbidden_tag_ids = set(TagService.get_forbidden_tag_ids(context.get("user"), action=context.get("action", "view")))
        context["forbidden_tag_ids"] = forbidden_tag_ids
    return [
        child
        for child in children
        if (context.get("include_hidden") or not child.is_hidden) and child.id not in forbidden_tag_ids
    ]


def _normalized_lines(content: str | None) -> list[str]:
    return [
        line.strip()
        for line in (content or "").splitlines()
        if line.strip()
    ]


def _serialize_forum_tag(tag, context: dict, include_children: bool = True) -> dict:
    payload = serialize_tag_base(tag, context)
    payload["can_start_discussion"] = resolve_tag_can_start_discussion(tag, context)
    payload["can_add_to_discussion"] = resolve_tag_can_add_to_discussion(tag, context)
    payload["can_reply"] = resolve_tag_can_reply(tag, context)
    payload["last_posted_discussion"] = resolve_tag_last_posted_discussion(tag, context)
    payload["canStartDiscussion"] = payload["can_start_discussion"]
    payload["canAddToDiscussion"] = payload["can_add_to_discussion"]
    payload["lastPostedDiscussion"] = payload["last_posted_discussion"]
    children = getattr(tag, "visible_children", []) if include_children else []
    payload["children"] = [
        _serialize_forum_tag(child, context, include_children=False)
        for child in children
    ]
    return payload

