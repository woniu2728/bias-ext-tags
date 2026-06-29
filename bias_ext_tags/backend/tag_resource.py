from __future__ import annotations

from bias_core.extensions import DatabaseResource, ResourceEndpoint, ResourceField, ResourceRelationship
from bias_ext_tags.backend.constants import EXTENSION_ID
from bias_ext_tags.backend.models import Tag


def tag_endpoint_specs() -> tuple[dict, ...]:
    from bias_ext_tags.backend.handlers import (
        dispatch_tag_create,
        dispatch_tag_index,
        dispatch_tag_popular,
        dispatch_tag_show_by_slug,
        dispatch_tag_update,
    )
    from bias_ext_tags.backend.handlers import core_delete_tag_response, core_show_tag_response

    return (
        {
            "name": "create",
            "handler": dispatch_tag_create,
            "methods": ("POST",),
            "path": "/tags",
            "absolute_path": True,
            "auth_required": True,
            "forum_permission": "tag.create",
        },
        {
            "name": "index",
            "handler": dispatch_tag_index,
            "methods": ("GET",),
            "path": "/tags",
            "absolute_path": True,
            "default_include": ("parent",),
        },
        {
            "name": "popular",
            "handler": dispatch_tag_popular,
            "methods": ("GET",),
            "path": "/tags/popular",
            "absolute_path": True,
        },
        {
            "name": "show",
            "methods": ("GET",),
            "path": "/tags/{object_id}",
            "absolute_path": True,
            "ability": "view",
            "kind": "show",
            "response_callback": core_show_tag_response,
        },
        {
            "name": "show-by-slug",
            "handler": dispatch_tag_show_by_slug,
            "methods": ("GET",),
            "path": "/tags/slug/{object_id}",
            "absolute_path": True,
        },
        {
            "name": "update",
            "handler": dispatch_tag_update,
            "methods": ("PATCH",),
            "path": "/tags/{object_id}",
            "absolute_path": True,
            "auth_required": True,
            "forum_permission": "tag.edit",
        },
        {
            "name": "delete",
            "methods": ("DELETE",),
            "path": "/tags/{object_id}",
            "absolute_path": True,
            "auth_required": True,
            "ability": "delete",
            "kind": "delete",
            "response_callback": core_delete_tag_response,
        },
    )


class TagResource(DatabaseResource):
    module_id = EXTENSION_ID
    model = Tag
    description = "论坛标签主资源。"

    def type(self) -> str:
        return "tag"

    def base(self, instance, context) -> dict:
        from bias_ext_tags.backend.resources import serialize_tag_base

        return serialize_tag_base(instance, context)

    def fields(self) -> list:
        return [
            ResourceField("name", resolver=lambda tag, context: tag.name, module_id=EXTENSION_ID)
            .string()
            .writable_when()
            .required_on_create_field()
            .max_length(100),
            ResourceField("description", resolver=lambda tag, context: tag.description, module_id=EXTENSION_ID)
            .string()
            .writable_when()
            .nullable_field()
            .max_length(700),
            ResourceField("slug", resolver=_resolve_tag_slug, module_id=EXTENSION_ID)
            .string()
            .writable_when()
            .required_on_create_field()
            .max_length(100)
            .regex(r"^[^/\\ ]*$"),
            ResourceField("storedSlug", resolver=lambda tag, context: tag.slug, module_id=EXTENSION_ID)
            .string()
            .visible_when(_can_view_tag_stored_slug),
            ResourceField("color", resolver=lambda tag, context: tag.color, module_id=EXTENSION_ID)
            .string()
            .writable_when()
            .nullable_field()
            .validate_with(_validate_tag_color),
            ResourceField("icon", resolver=lambda tag, context: tag.icon, module_id=EXTENSION_ID)
            .string()
            .writable_when()
            .nullable_field(),
            ResourceField("isHidden", resolver=lambda tag, context: tag.is_hidden, module_id=EXTENSION_ID)
            .boolean()
            .writable_when()
            .set_with(_set_tag_is_hidden),
            ResourceField("isPrimary", resolver=_resolve_tag_is_primary, module_id=EXTENSION_ID)
            .boolean()
            .writable_when()
            .set_with(_set_tag_is_primary),
            ResourceField("isRestricted", resolver=lambda tag, context: tag.is_restricted, module_id=EXTENSION_ID)
            .boolean()
            .writable_when(_tag_restriction_writable)
            .visible_when(_can_view_tag_admin_fields)
            .set_with(_set_tag_is_restricted),
            ResourceField("discussionCount", resolver=lambda tag, context: tag.discussion_count, module_id=EXTENSION_ID)
            .integer(),
            ResourceField("position", resolver=lambda tag, context: tag.position, module_id=EXTENSION_ID)
            .integer()
            .nullable_field(),
            ResourceField("defaultSort", resolver=lambda tag, context: tag.default_sort, module_id=EXTENSION_ID)
            .string()
            .writable_when()
            .nullable_field()
            .set_with(_set_tag_default_sort),
            ResourceField("isChild", resolver=lambda tag, context: bool(tag.parent_id), module_id=EXTENSION_ID)
            .boolean(),
            ResourceField("lastPostedAt", resolver=lambda tag, context: tag.last_posted_at, module_id=EXTENSION_ID),
            ResourceField("lastPostedDiscussion", resolver=_resolve_tag_last_posted_discussion_summary, module_id=EXTENSION_ID)
            .visible_when(_plain_tag_response_visible),
            ResourceField("canStartDiscussion", resolver=_resolve_tag_can_start_discussion, module_id=EXTENSION_ID)
            .boolean(),
            ResourceField("canAddToDiscussion", resolver=_resolve_tag_can_add_to_discussion, module_id=EXTENSION_ID)
            .boolean(),
            ResourceField("default_sort", resolver=lambda tag, context: tag.default_sort, module_id=EXTENSION_ID)
            .string()
            .writable_when()
            .nullable_field(),
            ResourceField("is_hidden", resolver=lambda tag, context: tag.is_hidden, module_id=EXTENSION_ID)
            .boolean()
            .writable_when(),
            ResourceField("is_primary", resolver=_resolve_tag_is_primary, module_id=EXTENSION_ID)
            .boolean()
            .writable_when()
            .set_with(_set_tag_is_primary),
            ResourceField("is_restricted", resolver=lambda tag, context: tag.is_restricted, module_id=EXTENSION_ID)
            .boolean()
            .writable_when(_tag_restriction_writable)
            .visible_when(_can_view_tag_admin_fields),
        ]

    def endpoints(self) -> list:
        return [
            ResourceEndpoint(module_id=EXTENSION_ID, **spec)
            for spec in tag_endpoint_specs()
        ]

    def relationships(self) -> list:
        return [
            ResourceRelationship("parent", resolver=_resolve_tag_parent, module_id=EXTENSION_ID)
            .to_one("tag")
            .set_relationship_with(_set_tag_parent_relationship)
            .writable_when(_tag_parent_relationship_writable),
            ResourceRelationship("children", resolver=_resolve_tag_children, module_id=EXTENSION_ID)
            .to_many("tag"),
            ResourceRelationship("lastPostedDiscussion", resolver=_resolve_tag_last_posted_discussion, module_id=EXTENSION_ID)
            .to_one("discussion"),
        ]

    def query(self, context):
        return Tag.objects.select_related("last_posted_discussion", "parent")

    def scope(self, queryset, context):
        from bias_ext_tags.backend.services import TagService

        action = context.get("action") or context.get("purpose") or "view"
        user = context.get("user")
        return TagService.filter_tags_for_user(queryset, user, action=action)

    def find(self, object_id: str, context):
        from bias_ext_tags.backend.services import TagService

        normalized = str(object_id or "").strip()
        if normalized.isdigit():
            tag = self.query(context).filter(id=int(normalized)).first()
            if tag is not None:
                return tag

        tag = TagService.get_tag_by_url_slug(normalized)
        if tag is None:
            tag = TagService.get_tag_by_url_slug(normalized, driver="id_with_slug")
        return tag

    def can(self, user, ability: str, instance, context) -> bool:
        from django.core.exceptions import PermissionDenied

        from bias_core.extensions.runtime import has_runtime_forum_permission
        from bias_ext_tags.backend.services import TagService

        if ability in {"create", "createTag", "tag.create"}:
            return bool(user and getattr(user, "is_authenticated", False) and has_runtime_forum_permission(user, "tag.create"))
        if ability in {"edit", "update", "tag.edit"}:
            return TagService.can_manage_tags(user, "tag.edit")
        if ability in {"delete", "tag.delete"}:
            return TagService.can_manage_tags(user, "tag.delete")
        if ability in {"view", "viewForum"} and instance is not None:
            if not TagService.can_view_tag(instance, user):
                raise PermissionDenied("没有权限查看此标签")
            return True
        return super().can(user, ability, instance, context)

    def delete_action(self, instance, context) -> None:
        from bias_ext_tags.backend.services import TagService

        TagService.delete_tag(instance.id, context.get("user"))


def _resolve_tag_slug(tag, context) -> str:
    from bias_ext_tags.backend.resources import resolve_tag_slug

    return resolve_tag_slug(tag, context)


def _can_view_tag_stored_slug(tag, context) -> bool:
    from bias_ext_tags.backend.resources import can_view_tag_stored_slug

    return can_view_tag_stored_slug(tag, context)


def _can_view_tag_admin_fields(tag, context) -> bool:
    from bias_ext_tags.backend.resources import can_view_tag_admin_fields

    return can_view_tag_admin_fields(context)


def _resolve_tag_is_primary(tag, context) -> bool:
    from bias_ext_tags.backend.services import TagService

    return TagService.is_primary_tree_tag(tag)


def _resolve_tag_can_start_discussion(tag, context) -> bool:
    from bias_ext_tags.backend.resources import resolve_tag_can_start_discussion

    return resolve_tag_can_start_discussion(tag, context)


def _resolve_tag_can_add_to_discussion(tag, context) -> bool:
    from bias_ext_tags.backend.resources import resolve_tag_can_add_to_discussion

    return resolve_tag_can_add_to_discussion(tag, context)


def _resolve_tag_parent(tag, context):
    from bias_ext_tags.backend.resources import resolve_tag_parent

    return resolve_tag_parent(tag, context)


def _resolve_tag_children(tag, context):
    from bias_ext_tags.backend.resources import resolve_tag_children

    return resolve_tag_children(tag, context)


def _resolve_tag_last_posted_discussion(tag, context):
    from bias_ext_tags.backend.resources import resolve_tag_last_posted_discussion_resource

    return resolve_tag_last_posted_discussion_resource(tag, context)


def _resolve_tag_last_posted_discussion_summary(tag, context):
    from bias_ext_tags.backend.resources import resolve_tag_last_posted_discussion

    return resolve_tag_last_posted_discussion(tag, context)


def _plain_tag_response_visible(tag, context) -> bool:
    request = context.get("request") if isinstance(context, dict) else None
    accept = str(getattr(request, "META", {}).get("HTTP_ACCEPT", "") or "")
    return "application/vnd.api+json" not in accept.lower()


def _tag_restriction_writable(tag, context) -> bool:
    return not bool(context.get("creating"))


def _tag_parent_relationship_writable(tag, context) -> bool:
    from bias_ext_tags.backend.resources import tag_parent_relationship_writable

    return tag_parent_relationship_writable(tag, context)


def _set_tag_parent_relationship(tag, value, context) -> None:
    from bias_ext_tags.backend.resources import set_tag_parent_relationship

    set_tag_parent_relationship(tag, value, context)


def _set_tag_is_hidden(tag, value, context) -> None:
    tag.is_hidden = value


def _set_tag_is_restricted(tag, value, context) -> None:
    tag.is_restricted = value


def _set_tag_default_sort(tag, value, context) -> None:
    tag.default_sort = value


def _set_tag_is_primary(tag, value, context) -> None:
    tag.is_primary = value
    if value is False:
        tag.position = None
        tag.parent_id = None


def _validate_tag_color(value, context) -> None:
    import re

    if value is None or re.match(r"^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$", value):
        return
    raise ValueError("color must be a valid hex color")
