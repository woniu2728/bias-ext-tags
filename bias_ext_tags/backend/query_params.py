from __future__ import annotations

from bias_core.extensions.platform import ResourceQueryOptions, merge_resource_includes, parse_resource_query_options
from bias_ext_tags.backend.models import Tag
from bias_ext_tags.backend.services import TagService


def tag_resource_options(context, resource: str = "tag") -> ResourceQueryOptions:
    options = context.get("resource_options") or parse_resource_query_options(context["request"], resource)
    default_include = tuple(context.get("default_include") or ())
    if not default_include:
        return options
    return ResourceQueryOptions(
        includes=merge_resource_includes(default_include, options.includes),
        fields=options.fields,
    )


def tag_query_value(context, key: str, default=None):
    return dict(context.get("query") or {}).get(key, default)


def tag_int_query_value(context, key: str):
    value = tag_query_value(context, key)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def tag_bool_query_value(context, key: str, default=False):
    value = tag_query_value(context, key)
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


def can_include_hidden_tags(user) -> bool:
    return TagService.can_manage_tags(user, "tag.edit")


def tag_purpose_query_value(context):
    purpose = str(tag_query_value(context, "purpose", "view") or "view")
    if purpose not in {"view", "start_discussion", "add_to_discussion", "reply"}:
        return "view"
    return purpose


def tag_current_discussion_tag_ids(context) -> tuple[int, ...]:
    discussion_id = tag_int_query_value(context, "discussion_id")
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
