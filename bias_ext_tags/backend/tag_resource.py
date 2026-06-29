from __future__ import annotations

from bias_core.extensions import DatabaseResource, ResourceEndpoint, ResourceField, ResourceRelationship
from bias_ext_tags.backend.constants import EXTENSION_ID
from bias_ext_tags.backend.models import Tag


def tag_endpoint_specs() -> tuple[dict, ...]:
    from bias_ext_tags.backend.responses import (
        dispatch_tag_popular,
    )
    from bias_ext_tags.backend.responses import (
        core_delete_tag_response,
        core_index_tag_response,
        core_show_tag_response,
        core_write_tag_response,
    )

    return (
        {
            "name": "create",
            "methods": ("POST",),
            "path": "/tags",
            "absolute_path": True,
            "auth_required": True,
            "ability": "create",
            "kind": "create",
            "forum_permission": "tag.create",
            "plain_response_callback": core_write_tag_response,
        },
        {
            "name": "index",
            "methods": ("GET",),
            "path": "/tags",
            "absolute_path": True,
            "kind": "index",
            "default_include": ("parent",),
            "plain_response_callback": core_index_tag_response,
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
            "plain_response_callback": core_show_tag_response,
        },
        {
            "name": "show-by-slug",
            "methods": ("GET",),
            "path": "/tags/slug/{object_id}",
            "absolute_path": True,
            "ability": "view",
            "kind": "show",
            "plain_response_callback": core_show_tag_response,
        },
        {
            "name": "update",
            "methods": ("PATCH",),
            "path": "/tags/{object_id}",
            "absolute_path": True,
            "auth_required": True,
            "ability": "edit",
            "kind": "update",
            "forum_permission": "tag.edit",
            "plain_response_callback": core_write_tag_response,
        },
        {
            "name": "delete",
            "methods": ("DELETE",),
            "path": "/tags/{object_id}",
            "absolute_path": True,
            "auth_required": True,
            "ability": "delete",
            "kind": "delete",
            "plain_response_callback": core_delete_tag_response,
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
            .unique(Tag, "slug")
            .regex(r"^[^/\\ ]*$"),
            ResourceField("storedSlug", resolver=lambda tag, context: tag.slug, module_id=EXTENSION_ID)
            .string()
            .visible_when(_can_view_tag_stored_slug),
            ResourceField("color", resolver=lambda tag, context: tag.color, module_id=EXTENSION_ID)
            .string()
            .writable_when()
            .nullable_field()
            .hex_color(),
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
            .writable_on_update_field()
            .visible_when(_can_view_tag_admin_fields)
            .set_with(_set_tag_is_restricted),
            ResourceField("discussionCount", resolver=lambda tag, context: tag.discussion_count, module_id=EXTENSION_ID)
            .integer(),
            ResourceField("position", resolver=lambda tag, context: tag.position, module_id=EXTENSION_ID)
            .integer()
            .nullable_field(),
            ResourceField("defaultSort", resolver=lambda tag, context: tag.default_sort, module_id=EXTENSION_ID)
            .string()
            .nullable_field(),
            ResourceField("isChild", resolver=lambda tag, context: bool(tag.parent_id), module_id=EXTENSION_ID)
            .boolean(),
            ResourceField("lastPostedAt", resolver=lambda tag, context: tag.last_posted_at, module_id=EXTENSION_ID),
            ResourceField("lastPostedDiscussion", resolver=_resolve_tag_last_posted_discussion_summary, module_id=EXTENSION_ID)
            .plain_only(),
            ResourceField("canStartDiscussion", resolver=_resolve_tag_can_start_discussion, module_id=EXTENSION_ID)
            .boolean(),
            ResourceField("canAddToDiscussion", resolver=_resolve_tag_can_add_to_discussion, module_id=EXTENSION_ID)
            .boolean(),
            ResourceField("default_sort", resolver=lambda tag, context: tag.default_sort, module_id=EXTENSION_ID)
            .string()
            .writable_when()
            .nullable_field()
            .plain_only(),
            ResourceField("is_hidden", resolver=lambda tag, context: tag.is_hidden, module_id=EXTENSION_ID)
            .boolean()
            .writable_when()
            .plain_only(),
            ResourceField("is_primary", resolver=_resolve_tag_is_primary, module_id=EXTENSION_ID)
            .boolean()
            .writable_when()
            .set_with(_set_tag_is_primary)
            .plain_only(),
            ResourceField("is_restricted", resolver=lambda tag, context: tag.is_restricted, module_id=EXTENSION_ID)
            .boolean()
            .writable_when()
            .visible_when(_can_view_tag_admin_fields)
            .plain_only(),
            ResourceField("parent_id", resolver=lambda tag, context: tag.parent_id, module_id=EXTENSION_ID)
            .integer()
            .writable_when()
            .nullable_field()
            .plain_only(),
            ResourceField("parentId", resolver=lambda tag, context: tag.parent_id, module_id=EXTENSION_ID)
            .integer()
            .writable_when()
            .nullable_field()
            .set_with(_set_tag_parent_id)
            .plain_only(),
        ]

    def endpoints(self) -> list:
        return [
            ResourceEndpoint(module_id=EXTENSION_ID, **spec)
            for spec in tag_endpoint_specs()
        ]

    def accepts_legacy_payload(self, context) -> bool:
        return True

    def jsonapi_types(self) -> tuple[str, ...]:
        return ("tag", "tags")

    def jsonapi_type(self) -> str:
        return "tags"

    def relationships(self) -> list:
        return [
            ResourceRelationship("parent", resolver=_resolve_tag_parent, module_id=EXTENSION_ID)
            .to_one("tag")
            .nullable_field()
            .set_relationship_with(_set_tag_parent_relationship)
            .writable_when(_tag_parent_relationship_writable),
            ResourceRelationship("children", resolver=_resolve_tag_children, module_id=EXTENSION_ID)
            .to_many("tag"),
            ResourceRelationship("lastPostedDiscussion", resolver=_resolve_tag_last_posted_discussion, module_id=EXTENSION_ID)
            .to_one("discussion"),
        ]

    def query(self, context):
        from django.db.models import Prefetch, Q

        from bias_ext_tags.backend.query_params import (
            can_include_hidden_tags,
            tag_bool_query_value,
            tag_current_discussion_tag_ids,
            tag_int_query_value,
            tag_purpose_query_value,
            tag_resource_options,
        )
        from bias_ext_tags.backend.services import TagService

        user = context.get("user")
        purpose = tag_purpose_query_value(context)
        resource_options = tag_resource_options(context)
        include_hidden = tag_bool_query_value(context, "include_hidden", False)
        include_children = tag_bool_query_value(context, "include_children", True)
        children_requested = include_children or "children" in resource_options.includes
        discussion_tag_ids = tag_current_discussion_tag_ids(context) if purpose == "add_to_discussion" else ()

        if include_hidden and not can_include_hidden_tags(user):
            include_hidden = False

        context["action"] = purpose
        context["resource_options"] = resource_options
        context["include_hidden"] = include_hidden
        context["include_children"] = include_children
        context["children_requested"] = children_requested
        context["discussion_tag_ids"] = discussion_tag_ids
        context.setdefault("_tag_cache", {})

        queryset = Tag.objects.select_related("last_posted_discussion", "parent").all()
        if children_requested:
            visible_child_queryset = Tag.objects.select_related("last_posted_discussion").order_by(*TagService.child_order_by())
            if not include_hidden:
                visible_child_queryset = visible_child_queryset.filter(is_hidden=False)
            visible_child_queryset = TagService.filter_tags_for_user(visible_child_queryset, user, action=purpose)
            if discussion_tag_ids:
                visible_child_queryset = visible_child_queryset | Tag.objects.filter(id__in=discussion_tag_ids)
            queryset = queryset.prefetch_related(
                Prefetch("children", queryset=visible_child_queryset, to_attr="visible_children")
            )

        parent_id = tag_int_query_value(context, "parent_id")
        if parent_id is None:
            queryset = queryset.filter(parent__isnull=True)
        else:
            queryset = queryset.filter(parent_id=parent_id)
        if not include_hidden:
            queryset = queryset.filter(is_hidden=False)
        queryset = TagService.filter_tags_for_user(queryset, user, action=purpose)
        if discussion_tag_ids:
            queryset = queryset | Tag.objects.filter(
                Q(id__in=discussion_tag_ids) | Q(children__id__in=discussion_tag_ids)
            )
        context["tag_index_scope_applied"] = True
        return queryset

    def scope(self, queryset, context):
        from bias_ext_tags.backend.services import TagService

        if context.get("tag_index_scope_applied"):
            return queryset
        action = context.get("action") or context.get("purpose") or "view"
        user = context.get("user")
        return TagService.filter_tags_for_user(queryset, user, action=action)

    def results(self, queryset, context):
        from bias_ext_tags.backend.preloads import apply_tag_resource_preloads
        from bias_ext_tags.backend.services import TagService

        queryset = apply_tag_resource_preloads(
            queryset.distinct(),
            user=context.get("user"),
            action=context.get("action") or "view",
            resource_options=context.get("resource_options"),
        )
        return list(queryset.order_by(*TagService.structure_order_by()))

    def find(self, object_id: str, context):
        from bias_ext_tags.backend.services import TagService

        normalized = str(object_id or "").strip()
        if normalized.isdigit():
            tag = self._detail_queryset(context).filter(id=int(normalized)).first()
            if tag is not None:
                return tag

        tag = TagService.get_tag_by_url_slug(normalized)
        if tag is None:
            tag = TagService.get_tag_by_url_slug(normalized, driver="id_with_slug")
        if tag is not None:
            return self._detail_queryset(context).filter(id=tag.id).first()
        return tag

    def _detail_queryset(self, context):
        from bias_ext_tags.backend.services import TagService

        queryset = Tag.objects.select_related("last_posted_discussion", "parent")
        return TagService.prefetch_state_for_user(queryset, context.get("user"))

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

    def creating(self, instance, context):
        from bias_ext_tags.backend.events import TagCreatingEvent

        _dispatch_tag_lifecycle_event(TagCreatingEvent(instance, context.get("user"), _request_body(context)))
        return instance

    def saving(self, instance, context):
        from bias_ext_tags.backend.events import TagSavingEvent

        _dispatch_tag_lifecycle_event(TagSavingEvent(instance, context.get("user"), _request_body(context)))
        return instance

    def deleting(self, instance, context) -> None:
        from bias_ext_tags.backend.events import TagDeletingEvent

        _dispatch_tag_lifecycle_event(TagDeletingEvent(instance, context.get("user")))

    def create_action(self, instance, context):
        from bias_ext_tags.backend.services import TagService

        instance = self.creating(instance, context) or instance
        instance = self.saving(instance, context) or instance
        payload = _service_payload_from_instance(instance, context, creating=True)
        return TagService.create_tag(user=context.get("user"), **payload)

    def update_action(self, instance, context):
        from bias_ext_tags.backend.services import TagService

        original_values = _capture_tag_lifecycle_values(instance)
        instance = self.saving(instance, context) or instance
        payload = _service_payload_from_instance(instance, context, creating=False)
        payload.update(_changed_tag_lifecycle_values(instance, original_values))
        return TagService.update_tag(tag_id=instance.id, user=context.get("user"), **payload)

    def delete_action(self, instance, context) -> None:
        from bias_ext_tags.backend.services import TagService

        self.deleting(instance, context)
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


def _service_payload_from_instance(tag, context, *, creating: bool) -> dict:
    attributes = _request_attributes(context)
    output = {
        "name": tag.name,
        "slug": tag.slug,
        "description": tag.description,
        "color": tag.color,
        "icon": tag.icon,
        "background_url": getattr(tag, "background_url", ""),
        "position": tag.position,
        "default_sort": tag.default_sort,
        "is_primary": tag.is_primary,
        "parent_id": tag.parent_id,
        "is_hidden": tag.is_hidden,
        "is_restricted": tag.is_restricted,
        "view_scope": getattr(tag, "view_scope", "public"),
        "start_discussion_scope": getattr(tag, "start_discussion_scope", "members"),
        "reply_scope": getattr(tag, "reply_scope", "members"),
    }
    if creating:
        output["parent_id"] = tag.parent_id
        return output

    requested = {
        "name": "name",
        "slug": "slug",
        "description": "description",
        "color": "color",
        "icon": "icon",
        "background_url": "background_url",
        "backgroundUrl": "background_url",
        "position": "position",
        "default_sort": "default_sort",
        "defaultSort": "default_sort",
        "is_primary": "is_primary",
        "isPrimary": "is_primary",
        "is_hidden": "is_hidden",
        "isHidden": "is_hidden",
        "is_restricted": "is_restricted",
        "isRestricted": "is_restricted",
        "view_scope": "view_scope",
        "viewScope": "view_scope",
        "start_discussion_scope": "start_discussion_scope",
        "startDiscussionScope": "start_discussion_scope",
        "reply_scope": "reply_scope",
        "replyScope": "reply_scope",
        "parent_id": "parent_id",
        "parentId": "parent_id",
    }
    requested_values = {target: output[target] for source, target in requested.items() if source in attributes}
    if _request_includes_parent_relationship(context):
        requested_values["parent_id"] = tag.parent_id
    return requested_values


def _dispatch_tag_lifecycle_event(event) -> None:
    from bias_core.extensions.platform import get_runtime_forum_event_bus

    get_runtime_forum_event_bus().dispatch(event)


def _request_body(context):
    if not isinstance(context, dict):
        return {}
    payload = context.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


def _capture_tag_lifecycle_values(tag) -> dict:
    return {
        field: getattr(tag, field, None)
        for field in _TAG_LIFECYCLE_MUTABLE_FIELDS
    }


def _changed_tag_lifecycle_values(tag, original_values: dict) -> dict:
    changed = {}
    for field in _TAG_LIFECYCLE_MUTABLE_FIELDS:
        current = getattr(tag, field, None)
        if current == original_values.get(field):
            continue
        changed[_TAG_LIFECYCLE_SERVICE_FIELDS[field]] = current
    return changed


_TAG_LIFECYCLE_MUTABLE_FIELDS = (
    "name",
    "slug",
    "description",
    "color",
    "icon",
    "background_url",
    "position",
    "default_sort",
    "is_primary",
    "parent_id",
    "is_hidden",
    "is_restricted",
    "view_scope",
    "start_discussion_scope",
    "reply_scope",
)

_TAG_LIFECYCLE_SERVICE_FIELDS = {
    "name": "name",
    "slug": "slug",
    "description": "description",
    "color": "color",
    "icon": "icon",
    "background_url": "background_url",
    "position": "position",
    "default_sort": "default_sort",
    "is_primary": "is_primary",
    "parent_id": "parent_id",
    "is_hidden": "is_hidden",
    "is_restricted": "is_restricted",
    "view_scope": "view_scope",
    "start_discussion_scope": "start_discussion_scope",
    "reply_scope": "reply_scope",
}


def _request_attributes(context) -> dict:
    payload = context.get("payload") if isinstance(context, dict) else None
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        attributes = data.get("attributes")
        return dict(attributes) if isinstance(attributes, dict) else {}
    return dict(payload) if isinstance(payload, dict) else {}


def _request_includes_parent_relationship(context) -> bool:
    payload = context.get("payload") if isinstance(context, dict) else None
    data = payload.get("data") if isinstance(payload, dict) else None
    relationships = data.get("relationships") if isinstance(data, dict) else None
    return isinstance(relationships, dict) and "parent" in relationships


def _set_tag_parent_id(tag, value, context) -> None:
    tag.parent_id = value
