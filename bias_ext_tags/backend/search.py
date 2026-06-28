from __future__ import annotations

from django.db.models import Q


def parse_tag_search_filter(token: str) -> str | None:
    if not token or ":" not in token:
        return None

    prefix, value = token.split(":", 1)
    if prefix.lower() != "tag":
        return None

    normalized = value.strip().lower()
    return normalized or None


def apply_discussion_tag_search_filter(queryset, tag_slug: str, context: dict):
    return queryset.filter(discussion_tags__tag__slug=tag_slug)


def apply_discussion_tag_list_query(queryset, context: dict):
    tag_slug = _query_param_value((context or {}).get("params"), "tag")
    if not tag_slug:
        return queryset
    return apply_discussion_tag_search_filter(queryset, tag_slug, context)


def hide_hidden_tag_discussions_from_all_list(queryset, context: dict):
    context = context or {}
    params = context.get("params")
    if (
        _has_value(context.get("query"))
        or _has_value(context.get("author"))
        or _query_param_value(params, "tag")
        or str(context.get("filter") or "all").strip().lower() != "all"
    ):
        return queryset

    from bias_ext_tags.backend.models import DiscussionTag

    hidden_discussion_ids = DiscussionTag.objects.filter(
        tag__is_hidden=True,
    ).values("discussion_id")
    return queryset.exclude(id__in=hidden_discussion_ids)


def apply_tag_fulltext_search(state, query: str, context: dict):
    value = str(query or "").strip()
    if not value:
        return state
    return state.filter(Q(name__istartswith=value) | Q(slug__istartswith=value))


def search_tags(queryset, criteria, context: dict):
    return queryset.order_by("position", "name", "id")


def _query_param_value(params, key: str) -> str:
    if not isinstance(params, dict):
        return ""
    value = params.get(key)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "").strip().lower()


def _has_value(value) -> bool:
    return bool(str(value or "").strip())
