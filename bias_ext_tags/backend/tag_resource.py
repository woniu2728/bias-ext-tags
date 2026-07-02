from __future__ import annotations

from bias_core.extensions import DatabaseResource, ResourceEndpoint, ResourceField, ResourceRelationship
from bias_core.extensions.platform import wants_jsonapi_response
from bias_ext_tags.backend.constants import EXTENSION_ID
from bias_ext_tags.backend.models import Tag


def _get_runtime_service(service_key: str, default=None):
    from bias_core.extensions.runtime import get_runtime_service

    return get_runtime_service(service_key, default)


def _runtime_service_method(service_key: str, name: str):
    service = _get_runtime_service(service_key)
    if isinstance(service, dict):
        method = service.get(name)
    else:
        method = getattr(service, name, None)
    if not callable(method):
        raise RuntimeError(f"Tags 扩展运行时服务缺少方法: {service_key}.{name}")
    return method


def tag_endpoint_specs() -> tuple[ResourceEndpoint, ...]:
    from bias_ext_tags.backend.responses import (
        dispatch_tag_index,
        dispatch_tag_popular,
    )
    from bias_ext_tags.backend.responses import (
        core_delete_tag_response,
        core_index_tag_response,
        core_show_tag_response,
        core_write_tag_response,
    )

    return (
        ResourceEndpoint.create()
        .at("/tags", absolute=True)
        .authenticated()
        .can("create")
        .requires_permission("tag.create")
        .plain_response(core_write_tag_response),
        ResourceEndpoint.index()
        .at("/tags", absolute=True)
        .add_default_include(("parent",))
        .with_handler(dispatch_tag_index)
        .plain_response(core_index_tag_response),
        ResourceEndpoint.index("popular")
        .at("/tags/popular", absolute=True)
        .as_kind("")
        .with_handler(dispatch_tag_popular),
        ResourceEndpoint.show()
        .at("/tags/{object_id}", absolute=True)
        .select_related_with("last_posted_discussion", "last_posted_user", "parent")
        .can("view")
        .plain_response(core_show_tag_response),
        ResourceEndpoint.show("show-by-slug")
        .at("/tags/slug/{object_id}", absolute=True)
        .select_related_with("last_posted_discussion", "last_posted_user", "parent")
        .can("view")
        .plain_response(core_show_tag_response),
        ResourceEndpoint.update()
        .with_methods("PATCH")
        .at("/tags/{object_id}", absolute=True)
        .authenticated()
        .can("edit")
        .requires_permission("tag.edit")
        .plain_response(core_write_tag_response),
        ResourceEndpoint.delete()
        .at("/tags/{object_id}", absolute=True)
        .authenticated()
        .can("delete")
        .plain_response(core_delete_tag_response),
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
            ResourceField("lastPostedUser", resolver=_resolve_tag_last_posted_user_summary, module_id=EXTENSION_ID)
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
        return [endpoint.for_module(EXTENSION_ID) for endpoint in tag_endpoint_specs()]

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
            .with_foreign_key_linkage("parent_id", condition=_can_link_tag_parent_id)
            .set_relationship_with(_set_tag_parent_relationship)
            .writable_when(_tag_parent_relationship_writable),
            ResourceRelationship("children", resolver=_resolve_tag_children, module_id=EXTENSION_ID)
            .to_many("tag")
            .scope(_scope_tag_children_relationship)
            .prefetch_to("visible_children"),
            ResourceRelationship("lastPostedDiscussion", resolver=_resolve_tag_last_posted_discussion, module_id=EXTENSION_ID)
            .to_one("discussion"),
            ResourceRelationship("lastPostedUser", resolver=_resolve_tag_last_posted_user, module_id=EXTENSION_ID)
            .to_one("user_detail"),
        ]

    def query(self, context):
        from bias_ext_tags.backend.responses import prepare_tag_index_context, tag_index_queryset

        return tag_index_queryset(prepare_tag_index_context(context))

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

        endpoint = context.get("endpoint") or "show"
        registry = context.get("registry")
        queryset = Tag.objects.all()
        if registry is not None:
            plan = registry.build_endpoint_preload_plan(
                "tag",
                endpoint,
                {"method": context.get("method") or "GET", **dict(context)},
            )
            if plan.select_related:
                queryset = queryset.select_related(*plan.select_related)
        else:
            queryset = queryset.select_related("last_posted_discussion", "last_posted_user", "parent")
        return TagService.prefetch_state_for_user(queryset, context.get("user"))

    def can(self, user, ability: str, instance, context) -> bool:
        from django.core.exceptions import PermissionDenied

        from bias_ext_tags.backend.services import TagService

        if ability in {"create", "createTag", "tag.create"}:
            return bool(
                user
                and getattr(user, "is_authenticated", False)
                and _runtime_service_method("users.service", "has_forum_permission")(user, "tag.create")
            )
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
        from bias_ext_tags.backend.events import TagCreatedEvent

        instance = self.creating(instance, context) or instance
        payload = _service_payload_from_instance(instance, context, creating=True)
        tag = TagService.create_tag(user=context.get("user"), **payload)
        _dispatch_tag_lifecycle_event(TagCreatedEvent(tag, context.get("user"), _request_body(context)))
        return tag

    def update_action(self, instance, context):
        from bias_ext_tags.backend.services import TagService
        from bias_ext_tags.backend.events import TagSavedEvent

        original_values = capture_tag_persisted_values(instance)
        instance = self.saving(instance, context) or instance
        payload = _service_payload_from_instance(instance, context, creating=False)
        payload.update(_changed_tag_lifecycle_values(instance, original_values))
        saved_tag = TagService.update_tag(tag_id=instance.id, user=context.get("user"), **payload)
        changed_fields = changed_tag_lifecycle_field_names(saved_tag, original_values)
        if changed_fields:
            _dispatch_tag_lifecycle_event(
                TagSavedEvent(saved_tag, context.get("user"), _request_body(context), changed_fields=changed_fields)
            )
        return saved_tag

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


def _can_link_tag_parent_id(tag, context) -> bool:
    parent_id = getattr(tag, "parent_id", None)
    if not parent_id:
        return True
    user = context.get("user")
    if user and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
        return True
    field_cache = getattr(getattr(tag, "_state", None), "fields_cache", {})
    parent = field_cache.get("parent") if isinstance(field_cache, dict) else None
    if parent is not None:
        from bias_ext_tags.backend.services import TagService

        return TagService.can_view_tag(parent, user)

    cache = context.setdefault("_tag_parent_linkage_visibility", {})
    if parent_id not in cache:
        from bias_ext_tags.backend.services import TagService

        queryset = TagService.filter_tags_for_user(
            Tag.objects.filter(id=parent_id),
            user,
            action=context.get("action") or "view",
        )
        cache[parent_id] = queryset.exists()
    return bool(cache[parent_id])


def _resolve_tag_children(tag, context):
    from bias_ext_tags.backend.resources import resolve_tag_children

    return resolve_tag_children(tag, context)


def _scope_tag_children_relationship(queryset, context):
    from bias_ext_tags.backend.services import TagService

    output = queryset.select_related("last_posted_discussion", "last_posted_user").order_by(*TagService.child_order_by())
    if not context.get("include_hidden"):
        output = output.filter(is_hidden=False)
    output = TagService.filter_tags_for_user(
        output,
        context.get("user"),
        action=context.get("action") or context.get("purpose") or "view",
    )
    discussion_tag_ids = tuple(context.get("discussion_tag_ids") or ())
    if discussion_tag_ids:
        output = output | Tag.objects.filter(id__in=discussion_tag_ids)
    return output


def _resolve_tag_last_posted_discussion(tag, context):
    from bias_ext_tags.backend.resources import resolve_tag_last_posted_discussion_resource

    return resolve_tag_last_posted_discussion_resource(tag, context)


def _resolve_tag_last_posted_discussion_summary(tag, context):
    from bias_ext_tags.backend.resources import resolve_tag_last_posted_discussion

    return resolve_tag_last_posted_discussion(tag, context)


def _resolve_tag_last_posted_user(tag, context):
    from bias_ext_tags.backend.resources import resolve_tag_last_posted_user_resource

    return resolve_tag_last_posted_user_resource(tag, context)


def _resolve_tag_last_posted_user_summary(tag, context):
    from bias_ext_tags.backend.resources import resolve_tag_last_posted_user

    return resolve_tag_last_posted_user(tag, context)


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


def capture_tag_persisted_values(tag) -> dict:
    from bias_ext_tags.backend.models import Tag

    tag_id = getattr(tag, "id", None)
    if not tag_id:
        return _capture_tag_lifecycle_values(tag)
    persisted = Tag.objects.only(*_TAG_LIFECYCLE_MUTABLE_FIELDS).get(id=tag_id)
    return _capture_tag_lifecycle_values(persisted)


def _changed_tag_lifecycle_values(tag, original_values: dict) -> dict:
    changed = {}
    for field in _TAG_LIFECYCLE_MUTABLE_FIELDS:
        current = getattr(tag, field, None)
        if current == original_values.get(field):
            continue
        changed[_TAG_LIFECYCLE_SERVICE_FIELDS[field]] = current
    return changed


def changed_tag_lifecycle_field_names(tag, original_values: dict) -> tuple[str, ...]:
    return tuple(
        sorted(
            _TAG_LIFECYCLE_SERVICE_FIELDS[field]
            for field in _TAG_LIFECYCLE_MUTABLE_FIELDS
            if getattr(tag, field, None) != original_values.get(field)
        )
    )


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
