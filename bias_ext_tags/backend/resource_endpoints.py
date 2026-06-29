from __future__ import annotations

from bias_core.extensions.platform import ResourceQueryOptions, apply_resource_preloads, parse_resource_query_options, serialize_resource_jsonapi_response, serialize_resource_plain, wants_jsonapi_response
from bias_core.extensions.platform import merge_resource_includes
from bias_ext_tags.backend.models import Tag
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
        "_tag_cache": {},
        "plain_related_fields": {
            "discussion": ("id", "title", "slug", "last_post_number", "last_posted_at"),
        },
    }


def _get_forbidden_tag_ids(context, user=None, action="view"):
    cache = context.setdefault("_tag_cache", {})
    if "forbidden_tag_ids" not in cache:
        cache["forbidden_tag_ids"] = set(TagService.get_forbidden_tag_ids(user, action=action))
    context["forbidden_tag_ids"] = cache["forbidden_tag_ids"]
    return cache["forbidden_tag_ids"]


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
    payload = serialize_resource_plain(
        _get_resource_registry(),
        "tag",
        tag,
        context,
        resource_options=resource_options,
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


def _wants_jsonapi_response(context) -> bool:
    return wants_jsonapi_response(context)


def _jsonapi_serialize_context(context, *, action="view") -> dict:
    output = _build_tag_serialize_context(context.get("user"), action=action)
    output.update({
        "request": context.get("request"),
        "query": dict(context.get("query") or {}),
        "resource_options": context.get("resource_options"),
        "default_include": tuple(context.get("default_include") or ()),
        "plain_related_fields": {
            "discussion": ("id", "title", "slug", "last_post_number", "last_posted_at"),
        },
    })
    return output


def _jsonapi_tag_response(tag, context, *, action="view", status=200):
    resource_options = _tag_resource_options(context)
    return serialize_resource_jsonapi_response(
        _get_resource_registry(),
        "tag",
        tag,
        _jsonapi_serialize_context(context, action=action),
        resource_options=resource_options,
        status=status,
    )


def _jsonapi_tags_response(tags, context, *, action="view"):
    resource_options = _tag_resource_options(context)
    return serialize_resource_jsonapi_response(
        _get_resource_registry(),
        "tag",
        tags,
        _jsonapi_serialize_context(context, action=action),
        resource_options=resource_options,
        many=True,
    )


def _apply_tag_resource_preloads(queryset, user=None, action="view", resource_options=None):
    resource_options = resource_options or ResourceQueryOptions()
    queryset = TagService.prefetch_state_for_user(queryset, user)
    return apply_resource_preloads(
        _get_resource_registry(),
        queryset,
        "tag",
        context={"user": user, "action": action},
        resource_options=resource_options,
    )


def _tag_query_value(context, key: str, default=None):
    return dict(context.get("query") or {}).get(key, default)


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
    tag_list = list(tags)
    jsonapi_response = _jsonapi_tags_response(tag_list, context, action="view")
    if jsonapi_response is not None:
        return jsonapi_response
    return {
        "data": [
            _serialize_tag(tag, user=user, context=serialize_context, resource_options=resource_options)
            for tag in tag_list
        ]
    }


def core_show_tag_response(context, response):
    user = context.get("user")
    tag = context.get("result")
    resource_options = _tag_resource_options(context)
    return _serialize_tag(tag, user=user, include_children=True, resource_options=resource_options)


def core_index_tag_response(context, response):
    user = context.get("user")
    tags = list(context.get("result") or [])
    resource_options = _tag_resource_options(context)
    action = context.get("action") or "view"

    serialize_context = _build_tag_serialize_context(user, action=action)
    serialize_context["include_hidden"] = bool(context.get("include_hidden"))
    return {
        "data": [
            _serialize_tag(
                tag,
                user=user,
                include_children=bool(context.get("include_children", True)),
                action=action,
                context=serialize_context,
                resource_options=resource_options,
            )
            for tag in tags
        ]
    }


def core_write_tag_response(context, response):
    tag = context.get("result")
    return _serialize_tag(tag, user=context.get("user"), include_children=True)


def core_delete_tag_response(context, response):
    return {"message": "标签已删除"}

