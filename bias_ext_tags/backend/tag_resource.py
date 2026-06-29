from __future__ import annotations

from bias_core.extensions import DatabaseResource, ResourceEndpoint
from bias_ext_tags.backend.constants import EXTENSION_ID
from bias_ext_tags.backend.models import Tag


def tag_endpoint_specs() -> tuple[dict, ...]:
    from bias_ext_tags.backend.handlers import (
        dispatch_tag_create,
        dispatch_tag_delete,
        dispatch_tag_index,
        dispatch_tag_popular,
        dispatch_tag_show,
        dispatch_tag_show_by_slug,
        dispatch_tag_update,
    )

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
            "handler": dispatch_tag_show,
            "methods": ("GET",),
            "path": "/tags/{object_id}",
            "absolute_path": True,
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
            "handler": dispatch_tag_delete,
            "methods": ("DELETE",),
            "path": "/tags/{object_id}",
            "absolute_path": True,
            "auth_required": True,
            "forum_permission": "tag.delete",
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

    def endpoints(self) -> list:
        return [
            ResourceEndpoint(module_id=EXTENSION_ID, **spec)
            for spec in tag_endpoint_specs()
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
            tag = self.scope(self.query(context), context).filter(id=int(normalized)).first()
            if tag is not None:
                return tag

        tag = TagService.get_tag_by_url_slug(normalized)
        if tag is None:
            tag = TagService.get_tag_by_url_slug(normalized, driver="id_with_slug")
        if tag is None:
            return None
        if not TagService.can_view_tag(tag, context.get("user")):
            return None
        return tag

    def can(self, user, ability: str, instance, context) -> bool:
        from bias_core.extensions.runtime import has_runtime_forum_permission
        from bias_ext_tags.backend.services import TagService

        if ability in {"create", "createTag", "tag.create"}:
            return bool(user and getattr(user, "is_authenticated", False) and has_runtime_forum_permission(user, "tag.create"))
        if ability in {"edit", "update", "tag.edit"}:
            return TagService.can_manage_tags(user, "tag.edit")
        if ability in {"delete", "tag.delete"}:
            return TagService.can_manage_tags(user, "tag.delete")
        if ability in {"view", "viewForum"} and instance is not None:
            return TagService.can_view_tag(instance, user)
        return super().can(user, ability, instance, context)
