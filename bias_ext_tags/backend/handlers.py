from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, JsonResponse
from ninja import Body

from bias_core.extensions.platform import api_error
from bias_core.extensions.platform import require_staff
from bias_core.extensions.platform import resolve_authenticated_user
from bias_core.extensions.platform import ResourceQueryOptions, parse_resource_query_options
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


def _wants_jsonapi_response(context) -> bool:
    request = context.get("request")
    accept = str(getattr(request, "META", {}).get("HTTP_ACCEPT", "") or "")
    return "application/vnd.api+json" in accept.lower()


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
    if not _wants_jsonapi_response(context):
        return None
    resource_options = _tag_resource_options(context)
    document = _get_resource_registry().serialize_jsonapi_document(
        "tag",
        tag,
        _jsonapi_serialize_context(context, action=action),
        only=resource_options.fields,
        include=resource_options.includes,
    )
    return JsonResponse(
        _flarum_jsonapi_document(document),
        status=status,
        content_type="application/vnd.api+json",
    )


def _jsonapi_tags_response(tags, context, *, action="view"):
    if not _wants_jsonapi_response(context):
        return None
    resource_options = _tag_resource_options(context)
    document = _get_resource_registry().serialize_jsonapi_document(
        "tag",
        tags,
        _jsonapi_serialize_context(context, action=action),
        only=resource_options.fields,
        include=resource_options.includes,
        many=True,
    )
    return JsonResponse(
        _flarum_jsonapi_document(document),
        content_type="application/vnd.api+json",
    )


def _flarum_jsonapi_document(value):
    if isinstance(value, list):
        return [_flarum_jsonapi_document(item) for item in value]
    if not isinstance(value, dict):
        return value
    output = {
        key: _flarum_jsonapi_document(item)
        for key, item in value.items()
    }
    resource_type = output.get("type")
    if resource_type in {"tag", "discussion"}:
        output["type"] = f"{resource_type}s"
    links = output.get("links")
    if isinstance(links, dict):
        self_link = links.get("self")
        if isinstance(self_link, str):
            links["self"] = (
                self_link
                .replace("/api/tag/", "/api/tags/")
                .replace("/api/discussion/", "/api/discussions/")
            )
    return output


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
    if _wants_jsonapi_response(context):
        if isinstance(response, dict):
            return JsonResponse(
                _flarum_jsonapi_document(response),
                content_type="application/vnd.api+json",
            )
        return response
    return _serialize_tag(tag, user=user, include_children=True, resource_options=resource_options)


def core_index_tag_response(context, response):
    user = context.get("user")
    tags = list(context.get("result") or [])
    resource_options = _tag_resource_options(context)
    action = context.get("action") or "view"
    if _wants_jsonapi_response(context):
        jsonapi_response = _jsonapi_tags_response(tags, context, action=action)
        if jsonapi_response is not None:
            return jsonapi_response
        return response

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
    if _wants_jsonapi_response(context):
        if isinstance(response, tuple):
            status, payload = response
            return JsonResponse(
                _flarum_jsonapi_document(payload),
                status=status,
                content_type="application/vnd.api+json",
            )
        if isinstance(response, dict):
            return JsonResponse(
                _flarum_jsonapi_document(response),
                content_type="application/vnd.api+json",
            )
        return response
    return _serialize_tag(tag, user=context.get("user"), include_children=True)


def core_delete_tag_response(context, response):
    if _wants_jsonapi_response(context):
        return response
    return {"message": "标签已删除"}


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

