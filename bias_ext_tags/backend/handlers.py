from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.db.models import Prefetch
from django.db.models import Q
from django.http import HttpResponse
from ninja import Body

from bias_core.extensions.platform import api_error
from bias_core.extensions.platform import require_staff
from bias_core.extensions.platform import resolve_authenticated_user
from bias_core.extensions.platform import ResourceQueryOptions, parse_resource_query_options
from bias_core.extensions.platform import merge_resource_includes
from bias_ext_tags.backend.models import Tag
from bias_ext_tags.backend.schemas import TagCreateSchema, TagUpdateSchema
from bias_ext_tags.backend.services import TagService


def get_runtime_resource_registry(*args, **kwargs):
    from bias_core.extensions.runtime import get_runtime_resource_registry as runtime_get_resource_registry

    return runtime_get_resource_registry(*args, **kwargs)


def _get_resource_registry():
    return get_runtime_resource_registry()


def _tag_resource_options(context, resource: str = "tag") -> ResourceQueryOptions:
    options = context.get("resource_options") or parse_resource_query_options(context["request"], resource)
    default_include = tuple(context.get("default_include") or ())
    if not default_include:
        return options
    return ResourceQueryOptions(
        includes=merge_resource_includes(default_include, options.includes),
        fields=options.fields,
    )


def _build_tag_serialize_context(user=None, action="view"):
    return {
        "user": user,
        "action": action,
        "forbidden_tag_ids": None,
        "plain_related_fields": {
            "discussion": ("id", "title", "slug", "last_post_number", "last_posted_at"),
        },
    }


def _get_forbidden_tag_ids(context, user=None, action="view"):
    if context.get("forbidden_tag_ids") is None:
        context["forbidden_tag_ids"] = set(TagService.get_forbidden_tag_ids(user, action=action))
    return context["forbidden_tag_ids"]


def _get_prefetched_children(tag):
    if hasattr(tag, "visible_children"):
        return tag.visible_children
    return tag.children.all().order_by(*TagService.child_order_by())


def _serialize_tag(
    tag,
    user=None,
    include_children=False,
    action="view",
    context=None,
    resource_options=None,
):
    context = context or _build_tag_serialize_context(user, action=action)
    context.setdefault("user", user)
    context.setdefault("action", action)
    resource_options = resource_options or ResourceQueryOptions()
    payload = _get_resource_registry().serialize(
        "tag",
        tag,
        context,
        only=resource_options.fields,
        include=resource_options.includes,
    )
    if "children" in resource_options.includes:
        return payload
    children = []
    if include_children:
        forbidden_tag_ids = _get_forbidden_tag_ids(context, user=user, action=action)
        children = [
            _serialize_tag(
                child,
                user=user,
                include_children=False,
                action=action,
                context=context,
                resource_options=resource_options,
            )
            for child in _get_prefetched_children(tag)
            if (context.get("include_hidden") or not child.is_hidden) and child.id not in forbidden_tag_ids
        ]

    payload["children"] = children
    return payload


def _apply_tag_resource_preloads(queryset, user=None, action="view", resource_options=None):
    resource_options = resource_options or ResourceQueryOptions()
    queryset = TagService.prefetch_state_for_user(queryset, user)
    return _get_resource_registry().apply_preload_plan(
        queryset,
        "tag",
        {"user": user, "action": action},
        only=resource_options.fields,
        include=resource_options.includes,
    )


def _tag_query_value(context, key: str, default=None):
    return dict(context.get("query") or {}).get(key, default)


def _tag_payload(context) -> dict:
    payload = context.get("payload")
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return dict(payload)

    attributes = data.get("attributes")
    normalized = dict(attributes) if isinstance(attributes, dict) else {}
    relationships = data.get("relationships")
    if isinstance(relationships, dict) and "parent" in relationships:
        normalized["parent_id"] = _tag_relationship_id(relationships.get("parent"))
    return normalized


def _tag_relationship_id(value):
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


def _tag_object_id(context) -> int:
    try:
        return int(context.get("object_id") or 0)
    except (TypeError, ValueError):
        return 0


def _tag_object_slug(context) -> str:
    return str(context.get("object_id") or "").strip()


def _tag_int_query_value(context, key: str):
    value = _tag_query_value(context, key)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _tag_bool_query_value(context, key: str, default=False):
    value = _tag_query_value(context, key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _can_include_hidden_tags(user) -> bool:
    return TagService.can_manage_tags(user, "tag.edit")


def _tag_purpose_query_value(context):
    purpose = str(_tag_query_value(context, "purpose", "view") or "view")
    if purpose not in {"view", "start_discussion", "add_to_discussion", "reply"}:
        return "view"
    return purpose


def dispatch_tag_create(context):
    payload = TagCreateSchema(**_tag_payload(context))
    try:
        tag = TagService.create_tag(
            name=payload.name,
            slug=payload.slug,
            description=payload.description or "",
            color=payload.color or "",
            icon=payload.icon or "",
            background_url=payload.background_url or "",
            position=payload.position,
            default_sort=payload.default_sort,
            is_primary=payload.is_primary,
            parent_id=payload.parent_id,
            is_hidden=payload.is_hidden or False,
            is_restricted=payload.is_restricted or False,
            view_scope=payload.view_scope or Tag.ACCESS_PUBLIC,
            start_discussion_scope=payload.start_discussion_scope or Tag.ACCESS_MEMBERS,
            reply_scope=payload.reply_scope or Tag.ACCESS_MEMBERS,
            user=context["user"],
        )
        return _serialize_tag(tag, user=context["user"], include_children=True)
    except PermissionDenied as e:
        return api_error(str(e), status=403)
    except ValueError as e:
        return api_error(str(e), status=400)


def dispatch_tag_index(context):
    request = context["request"]
    user = context.get("user")
    resource_options = _tag_resource_options(context)
    include_hidden = _tag_bool_query_value(context, "include_hidden", False)
    include_children = _tag_bool_query_value(context, "include_children", True)
    children_requested = include_children or "children" in resource_options.includes
    purpose = _tag_purpose_query_value(context)
    discussion_tag_ids = _tag_current_discussion_tag_ids(context) if purpose == "add_to_discussion" else ()
    if include_hidden and not _can_include_hidden_tags(user):
        include_hidden = False

    queryset = Tag.objects.select_related("last_posted_discussion").all()
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
    queryset = _apply_tag_resource_preloads(
        queryset,
        user=user,
        action=purpose,
        resource_options=resource_options,
    )

    parent_id = _tag_int_query_value(context, "parent_id")
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
    tags = queryset.distinct().order_by(*TagService.structure_order_by())

    serialize_context = _build_tag_serialize_context(user, action=purpose)
    serialize_context["include_hidden"] = include_hidden
    return {
        "data": [
            _serialize_tag(
                tag,
                user=user,
                include_children=include_children,
                action=purpose,
                context=serialize_context,
                resource_options=resource_options,
            )
            for tag in tags
        ]
    }


def _tag_current_discussion_tag_ids(context) -> tuple[int, ...]:
    discussion_id = _tag_int_query_value(context, "discussion_id")
    if not discussion_id:
        return ()
    user = context.get("user")
    visible_current_tags = TagService.filter_tags_for_user(
        Tag.objects.filter(discussion_tags__discussion_id=discussion_id),
        user,
        action="view",
    )
    return tuple(
        visible_current_tags.order_by("id")
        .values_list("id", flat=True)
    )


def dispatch_tag_popular(context):
    request = context["request"]
    user = context.get("user")
    resource_options = _tag_resource_options(context)
    limit = _tag_int_query_value(context, "limit") or 10
    tags = TagService.filter_tags_for_user(
        Tag.objects.filter(is_hidden=False),
        user,
        action="view",
    )
    tags = _apply_tag_resource_preloads(
        tags,
        user=user,
        action="view",
        resource_options=resource_options,
    ).order_by("-discussion_count", "-last_posted_at")[:limit]

    serialize_context = _build_tag_serialize_context(user, action="view")
    return {
        "data": [
            _serialize_tag(tag, user=user, context=serialize_context, resource_options=resource_options)
            for tag in tags
        ]
    }


def _load_visible_tag(tag, user, resource_options):
    if not tag:
        return None
    tag = _apply_tag_resource_preloads(
        Tag.objects.select_related("last_posted_discussion").prefetch_related("children").filter(id=tag.id),
        user=user,
        action="view",
        resource_options=resource_options,
    ).get()
    if not TagService.can_view_tag(tag, user):
        return api_error("没有权限查看此标签", status=403)
    return tag


def dispatch_tag_show(context):
    request = context["request"]
    user = context.get("user")
    resource_options = _tag_resource_options(context)
    tag = _resolve_tag_route_object(context)
    if tag is None:
        return api_error("标签不存在", status=404)
    tag = _load_visible_tag(tag, user, resource_options)
    if tag is None:
        return api_error("标签不存在", status=404)
    if hasattr(tag, "status_code"):
        return tag
    return _serialize_tag(tag, user=user, include_children=True, resource_options=resource_options)


def dispatch_tag_show_by_slug(context):
    request = context["request"]
    user = context.get("user")
    resource_options = _tag_resource_options(context)
    tag = _resolve_tag_route_object(context)
    tag = _load_visible_tag(tag, user, resource_options)
    if tag is None:
        return api_error("标签不存在", status=404)
    if hasattr(tag, "status_code"):
        return tag
    return _serialize_tag(tag, user=user, include_children=True, resource_options=resource_options)


def _resolve_tag_route_object(context):
    object_slug = _tag_object_slug(context)
    if object_slug.isdigit():
        tag = TagService.get_tag_by_id(int(object_slug))
        if tag is not None:
            return tag
    tag = TagService.get_tag_by_url_slug(object_slug)
    if tag is None:
        tag = TagService.get_tag_by_url_slug(object_slug, driver="id_with_slug")
    return tag


def dispatch_tag_update(context):
    raw_payload = _tag_payload(context)
    payload = TagUpdateSchema(**raw_payload)
    update_kwargs = {
        "tag_id": _tag_object_id(context),
        "user": context["user"],
        "name": payload.name,
        "slug": payload.slug,
        "description": payload.description,
        "color": payload.color,
        "icon": payload.icon,
        "background_url": payload.background_url,
        "position": payload.position,
        "default_sort": payload.default_sort,
        "is_primary": payload.is_primary,
        "is_hidden": payload.is_hidden,
        "is_restricted": payload.is_restricted,
        "view_scope": payload.view_scope,
        "start_discussion_scope": payload.start_discussion_scope,
        "reply_scope": payload.reply_scope,
    }
    if "parent_id" in raw_payload:
        update_kwargs["parent_id"] = payload.parent_id
    try:
        tag = TagService.update_tag(**update_kwargs)

        tag = Tag.objects.select_related("last_posted_discussion").prefetch_related("children").get(id=tag.id)
        return _serialize_tag(tag, user=context["user"], include_children=True)
    except Tag.DoesNotExist:
        return api_error("标签不存在", status=404)
    except PermissionDenied as e:
        return api_error(str(e), status=403)
    except ValueError as e:
        return api_error(str(e), status=400)


def dispatch_tag_delete(context):
    try:
        TagService.delete_tag(_tag_object_id(context), context["user"])
        return {"message": "标签已删除"}
    except Tag.DoesNotExist:
        return api_error("标签不存在", status=404)
    except PermissionDenied as e:
        return api_error(str(e), status=403)
    except ValueError as e:
        return api_error(str(e), status=400)


def order_tags_api_route(request, payload: dict = Body(...)):
    user = resolve_authenticated_user(request)
    if user is not None and getattr(user, "is_authenticated", False):
        request.auth = user

    denied = require_staff(request)
    if denied:
        return denied
    if not isinstance(payload, dict) or "order" not in payload:
        return HttpResponse(status=422)

    try:
        TagService.order_tags(payload.get("order"), request.auth)
        return HttpResponse(status=204)
    except PermissionDenied as e:
        return api_error(str(e), status=403)
    except ValueError as e:
        return api_error(str(e), status=400)

