from __future__ import annotations

from dataclasses import replace

from django.core.cache import cache
from django.db.models import Prefetch, Q

from bias_core.extensions.platform import ResourceQueryOptions, merge_resource_includes, serialize_resource_jsonapi_response, serialize_resource_plain, wants_jsonapi_response
from bias_ext_tags.backend.models import Tag
from bias_ext_tags.backend.preloads import apply_tag_resource_preloads
from bias_ext_tags.backend.query_params import (
    can_include_hidden_tags,
    tag_bool_query_value,
    tag_current_discussion_tag_ids,
    tag_int_query_value,
    tag_purpose_query_value,
    tag_resource_options,
)
from bias_ext_tags.backend.services import TagService


ANONYMOUS_TAG_INDEX_CACHE_VERSION_KEY = "tags:index:version"


def get_runtime_resource_registry(*args, **kwargs):
    from bias_core.extensions.runtime import get_runtime_resource_registry as runtime_get_resource_registry

    return runtime_get_resource_registry(*args, **kwargs)


def get_resource_registry():
    return get_runtime_resource_registry()


def build_tag_serialize_context(user=None, action="view"):
    return {
        "user": user,
        "action": action,
        "_tag_cache": {},
        "plain_related_fields": {
            "discussion": ("id", "title", "slug", "last_post_number", "last_posted_at"),
            "user_detail": ("id", "username", "display_name", "avatar_url"),
        },
    }


def get_forbidden_tag_ids(context, user=None, action="view"):
    cache = context.setdefault("_tag_cache", {})
    if "forbidden_tag_ids" not in cache:
        cache["forbidden_tag_ids"] = set(TagService.get_forbidden_tag_ids(user, action=action))
    context["forbidden_tag_ids"] = cache["forbidden_tag_ids"]
    return cache["forbidden_tag_ids"]


def serialize_tag(
    tag,
    user=None,
    include_children=False,
    action="view",
    context=None,
    resource_options=None,
):
    context = context or build_tag_serialize_context(user, action=action)
    context.setdefault("user", user)
    context.setdefault("action", action)
    resource_options = resource_options or ResourceQueryOptions()
    includes = resource_options.includes
    if include_children or "children" in includes:
        includes = merge_resource_includes(includes, ("children", "children.children"))
        context = {**context, "plain_children_depth": 1}
    child_resource_options = ResourceQueryOptions(
        includes=includes,
        fields=resource_options.fields,
    )
    payload = serialize_resource_plain(
        get_resource_registry(),
        "tag",
        tag,
        context,
        resource_options=child_resource_options,
    )
    payload.setdefault("children", [])
    return payload


def serialize_tag_index_fast(
    tag,
    *,
    context: dict,
    resource_options: ResourceQueryOptions,
    include_children: bool,
) -> dict:
    from bias_ext_tags.backend.resources import (
        can_view_tag_admin_fields,
        can_view_tag_stored_slug,
        resolve_tag_can_add_to_discussion,
        resolve_tag_can_reply,
        resolve_tag_can_start_discussion,
        resolve_tag_last_posted_discussion,
        resolve_tag_state,
    )

    payload = {
        "id": tag.id,
        "name": tag.name,
        "slug": str(getattr(tag, "slug", "") or "").strip(),
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

    payload["canStartDiscussion"] = resolve_tag_can_start_discussion(tag, context)
    payload["canAddToDiscussion"] = resolve_tag_can_add_to_discussion(tag, context)
    payload["can_start_discussion"] = payload["canStartDiscussion"]
    payload["can_add_to_discussion"] = payload["canAddToDiscussion"]
    payload["can_reply"] = resolve_tag_can_reply(tag, context)
    payload["lastPostedDiscussion"] = resolve_tag_last_posted_discussion(tag, context)
    payload["lastPostedUser"] = _serialize_last_posted_user_fast(tag, context)

    if _tag_index_includes(resource_options, "lastPostedDiscussion"):
        payload["last_posted_discussion"] = payload["lastPostedDiscussion"]
    if _tag_index_includes(resource_options, "lastPostedUser"):
        payload["last_posted_user"] = payload["lastPostedUser"]

    if _tag_index_includes(resource_options, "parent"):
        payload["parent"] = _serialize_tag_parent_fast(tag, context, resource_options)

    payload["children"] = []
    if include_children or _tag_index_includes(resource_options, "children"):
        payload["children"] = [
            serialize_tag_index_fast(
                child,
                context=context,
                resource_options=resource_options,
                include_children=False,
            )
            for child in _visible_prefetched_children(tag, context)
        ]
    return payload


def _serialize_last_posted_user_fast(tag, context: dict) -> dict | None:
    user = getattr(tag, "last_posted_user", None)
    if not user:
        return None
    cache = context.setdefault("_tag_user_summary_cache", {})
    user_id = getattr(user, "id", None)
    if user_id is None:
        return {
            "id": None,
            "username": getattr(user, "username", ""),
            "display_name": getattr(user, "display_name", "") or getattr(user, "username", ""),
            "avatar_url": getattr(user, "avatar_url", ""),
        }
    if user_id not in cache:
        cache[user_id] = {
            "id": user.id,
            "username": user.username,
            "display_name": getattr(user, "display_name", "") or getattr(user, "username", ""),
            "avatar_url": getattr(user, "avatar_url", ""),
        }
    return cache[user_id]


def _tag_index_includes(resource_options: ResourceQueryOptions, include: str) -> bool:
    return include in tuple(resource_options.includes or ())


def _serialize_tag_parent_fast(tag, context: dict, resource_options: ResourceQueryOptions) -> dict | None:
    parent = getattr(tag, "parent", None)
    if parent is None:
        return None
    return serialize_tag_index_fast(
        parent,
        context=context,
        resource_options=ResourceQueryOptions(fields=resource_options.fields),
        include_children=False,
    )


def _visible_prefetched_children(tag, context: dict) -> list:
    children = getattr(tag, "visible_children", None)
    if children is None:
        return []
    forbidden_tag_ids = get_forbidden_tag_ids(
        context,
        context.get("user"),
        action=context.get("action", "view"),
    )
    include_hidden = bool(context.get("include_hidden"))
    return [
        child
        for child in children
        if (include_hidden or not child.is_hidden) and child.id not in forbidden_tag_ids
    ]


def can_use_fast_tag_index_response(context: dict, resource_options: ResourceQueryOptions) -> bool:
    if wants_jsonapi_response(context):
        return False
    if resource_options.fields:
        return False
    supported_includes = {"parent", "children", "children.children", "lastPostedDiscussion", "lastPostedUser"}
    return set(resource_options.includes or ()).issubset(supported_includes)


def get_cached_anonymous_tag_index_response(
    context: dict,
    resource_options: ResourceQueryOptions,
    *,
    include_children: bool,
):
    if not can_use_anonymous_tag_index_cache(context, resource_options):
        return None
    key = anonymous_tag_index_cache_key(resource_options, include_children=include_children)
    return cache.get(key)


def set_cached_anonymous_tag_index_response(
    context: dict,
    resource_options: ResourceQueryOptions,
    payload: dict,
    *,
    include_children: bool,
) -> None:
    if not can_use_anonymous_tag_index_cache(context, resource_options):
        return
    key = anonymous_tag_index_cache_key(resource_options, include_children=include_children)
    cache.set(key, payload, timeout=30)


def anonymous_tag_index_cache_version() -> int:
    version = cache.get(ANONYMOUS_TAG_INDEX_CACHE_VERSION_KEY)
    if version is None:
        cache.add(ANONYMOUS_TAG_INDEX_CACHE_VERSION_KEY, 1, timeout=None)
        version = cache.get(ANONYMOUS_TAG_INDEX_CACHE_VERSION_KEY) or 1
    try:
        return int(version)
    except (TypeError, ValueError):
        cache.set(ANONYMOUS_TAG_INDEX_CACHE_VERSION_KEY, 1, timeout=None)
        return 1


def bump_anonymous_tag_index_cache_version() -> None:
    cache.add(ANONYMOUS_TAG_INDEX_CACHE_VERSION_KEY, 1, timeout=None)
    try:
        cache.incr(ANONYMOUS_TAG_INDEX_CACHE_VERSION_KEY)
    except ValueError:
        cache.set(ANONYMOUS_TAG_INDEX_CACHE_VERSION_KEY, 2, timeout=None)


def can_use_anonymous_tag_index_cache(context: dict, resource_options: ResourceQueryOptions) -> bool:
    user = context.get("user")
    if user and getattr(user, "is_authenticated", False):
        return False
    if not can_use_fast_tag_index_response(context, resource_options):
        return False
    if context.get("action") not in (None, "view"):
        return False
    if context.get("include_hidden"):
        return False
    if context.get("discussion_tag_ids"):
        return False
    return True


def anonymous_tag_index_cache_key(resource_options: ResourceQueryOptions, *, include_children: bool) -> str:
    parts = (
        "tags:index:v3",
        f"version={anonymous_tag_index_cache_version()}",
        f"children={int(bool(include_children))}",
        "include=" + ",".join(resource_options.includes or ()),
    )
    return "|".join(parts)


def wants_tag_jsonapi_response(context) -> bool:
    return wants_jsonapi_response(context)


def jsonapi_serialize_context(context, *, action="view") -> dict:
    output = build_tag_serialize_context(context.get("user"), action=action)
    output.update({
        "request": context.get("request"),
        "query": dict(context.get("query") or {}),
        "resource_options": context.get("resource_options"),
        "default_include": tuple(context.get("default_include") or ()),
        "plain_related_fields": {
            "discussion": ("id", "title", "slug", "last_post_number", "last_posted_at"),
            "user_detail": ("id", "username", "display_name", "avatar_url"),
        },
    })
    return output


def jsonapi_tag_response(tag, context, *, action="view", status=200):
    resource_options = tag_resource_options(context)
    return serialize_resource_jsonapi_response(
        get_resource_registry(),
        "tag",
        tag,
        jsonapi_serialize_context(context, action=action),
        resource_options=resource_options,
        status=status,
    )


def jsonapi_tags_response(tags, context, *, action="view"):
    resource_options = tag_resource_options(context)
    return serialize_resource_jsonapi_response(
        get_resource_registry(),
        "tag",
        tags,
        jsonapi_serialize_context(context, action=action),
        resource_options=resource_options,
        many=True,
    )


def dispatch_tag_popular(context):
    user = context.get("user")
    resource_options = tag_resource_options(context)
    limit = tag_int_query_value(context, "limit") or 10
    tags = TagService.filter_tags_for_user(
        Tag.objects.filter(is_hidden=False),
        user,
        action="view",
    )
    tags = apply_tag_resource_preloads(
        tags,
        user=user,
        action="view",
        resource_options=resource_options,
    ).order_by("-discussion_count", "-last_posted_at")[:limit]

    serialize_context = build_tag_serialize_context(user, action="view")
    tag_list = list(tags)
    jsonapi_response = jsonapi_tags_response(tag_list, context, action="view")
    if jsonapi_response is not None:
        return jsonapi_response
    return {
        "data": [
            serialize_tag(tag, user=user, context=serialize_context, resource_options=resource_options)
            for tag in tag_list
        ]
    }


def dispatch_tag_index(context):
    if wants_jsonapi_response(context):
        return _dispatch_tag_index_generic(context)

    prepared_context = prepare_tag_index_context(context)
    resource_options = tag_resource_options(prepared_context)
    include_children = bool(prepared_context.get("include_children", True))
    if not can_use_anonymous_tag_index_cache(prepared_context, resource_options):
        return _dispatch_tag_index_generic(prepared_context)

    cached = get_cached_anonymous_tag_index_response(
        prepared_context,
        resource_options,
        include_children=include_children,
    )
    if cached is not None:
        context["result"] = cached
        return cached

    tags = list_tag_index_results(prepared_context, resource_options=resource_options)
    prepared_context["result"] = tags
    response = core_index_tag_response(prepared_context, {"data": tags})
    prepared_context["result"] = response
    return response


def _dispatch_tag_index_generic(context):
    registry = context.get("registry") or context.get("api") or get_resource_registry()
    definition = registry.get_dispatch_endpoint(
        "tag",
        context.get("endpoint") or "index",
        context.get("method") or "GET",
        dict(context),
    )
    if definition is None:
        raise ValueError("资源端点不存在")
    from bias_core.resource_endpoint_runner import ResourceEndpointRunner

    return ResourceEndpointRunner(registry).run(replace(definition, handler=None), context)


def prepare_tag_index_context(context):
    purpose = tag_purpose_query_value(context)
    resource_options = tag_resource_options(context)
    include_hidden = tag_bool_query_value(context, "include_hidden", False)
    include_children = tag_bool_query_value(context, "include_children", True)
    children_requested = include_children or "children" in resource_options.includes
    user = context.get("user")
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
    return context


def list_tag_index_results(context, *, resource_options: ResourceQueryOptions | None = None) -> list:
    queryset = tag_index_queryset(context)
    queryset = apply_tag_resource_preloads(
        queryset.distinct(),
        user=context.get("user"),
        action=context.get("action") or "view",
        resource_options=resource_options or context.get("resource_options"),
    )
    return list(queryset.order_by(*TagService.structure_order_by()))


def tag_index_queryset(context):
    include_children = bool(context.get("include_children", True))
    children_requested = bool(context.get("children_requested", include_children))
    discussion_tag_ids = tuple(context.get("discussion_tag_ids") or ())

    queryset = Tag.objects.select_related("last_posted_discussion", "last_posted_user", "parent").all()
    if children_requested:
        queryset = queryset.prefetch_related(
            Prefetch(
                "children",
                queryset=_scope_tag_index_children(Tag.objects.all(), context),
                to_attr="visible_children",
            )
        )

    parent_id = tag_int_query_value(context, "parent_id")
    if parent_id is None and not wants_jsonapi_response(context):
        queryset = queryset.filter(parent__isnull=True)
    elif parent_id is not None:
        queryset = queryset.filter(parent_id=parent_id)
    if not context.get("include_hidden"):
        queryset = queryset.filter(is_hidden=False)
    queryset = TagService.filter_tags_for_user(
        queryset,
        context.get("user"),
        action=context.get("action") or "view",
    )
    if discussion_tag_ids:
        queryset = queryset | Tag.objects.filter(
            Q(id__in=discussion_tag_ids) | Q(children__id__in=discussion_tag_ids)
        )
    context["tag_index_scope_applied"] = True
    return queryset


def _scope_tag_index_children(queryset, context):
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


def core_show_tag_response(context, response):
    user = context.get("user")
    tag = context.get("result")
    resource_options = tag_resource_options(context)
    return serialize_tag(tag, user=user, include_children=True, resource_options=resource_options)


def core_index_tag_response(context, response):
    if isinstance(response, dict) and "data" in response and context.get("result") is response:
        return response
    user = context.get("user")
    tags = list(context.get("result") or [])
    resource_options = tag_resource_options(context)
    action = context.get("action") or "view"
    include_children = bool(context.get("include_children", True))

    cached = get_cached_anonymous_tag_index_response(
        context,
        resource_options,
        include_children=include_children,
    )
    if cached is not None:
        return cached

    serialize_context = build_tag_serialize_context(user, action=action)
    serialize_context["include_hidden"] = bool(context.get("include_hidden"))
    if can_use_fast_tag_index_response(context, resource_options):
        payload = {
            "data": [
                serialize_tag_index_fast(
                    tag,
                    context=serialize_context,
                    resource_options=resource_options,
                    include_children=include_children,
                )
                for tag in tags
            ]
        }
        set_cached_anonymous_tag_index_response(
            context,
            resource_options,
            payload,
            include_children=include_children,
        )
        return payload
    return {
        "data": [
            serialize_tag(
                tag,
                user=user,
                include_children=include_children,
                action=action,
                context=serialize_context,
                resource_options=resource_options,
            )
            for tag in tags
        ]
    }


def core_write_tag_response(context, response):
    tag = context.get("result")
    return serialize_tag(tag, user=context.get("user"), include_children=True)


def core_delete_tag_response(context, response):
    return {"message": "标签已删除"}
