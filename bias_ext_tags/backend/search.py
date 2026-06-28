from __future__ import annotations

from django.db.models import Exists, OuterRef, Q

from bias_core.extensions.runtime import resolve_runtime_model_slugs
from bias_ext_tags.backend.models import DiscussionTag, Tag


def parse_tag_search_filter(token: str) -> str | None:
    if not token or ":" not in token:
        return None

    prefix, value = token.split(":", 1)
    if prefix.lower() != "tag":
        return None

    normalized = value.strip().lower()
    return normalized or None


def apply_discussion_tag_search_filter(queryset, tag_slug: str, context: dict):
    return _apply_discussion_tag_filter(queryset, tag_slug, context)


def apply_discussion_tag_list_query(queryset, context: dict):
    tag_slug = _query_param_value((context or {}).get("params"), "tag")
    if not tag_slug:
        return queryset
    return _apply_discussion_tag_filter(queryset, tag_slug, context)


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


def _apply_discussion_tag_filter(queryset, raw_value: str, context: dict):
    groups = _tag_slug_groups(raw_value)
    if not groups:
        return queryset

    slug_to_id = _resolve_tag_slug_ids(groups, context)
    output = queryset
    for group in groups:
        condition = Q()
        for slug in group:
            if slug == "untagged":
                condition |= ~Exists(
                    DiscussionTag.objects.filter(discussion_id=OuterRef("pk"))
                )
                continue

            tag_id = slug_to_id.get(slug)
            if tag_id is None:
                continue
            condition |= Exists(
                DiscussionTag.objects.filter(
                    discussion_id=OuterRef("pk"),
                    tag_id=tag_id,
                )
            )

        if not condition:
            return queryset.none()
        output = output.filter(condition)
    return output


def _tag_slug_groups(raw_value: str) -> tuple[tuple[str, ...], ...]:
    groups: list[tuple[str, ...]] = []
    values = raw_value if isinstance(raw_value, (list, tuple)) else [raw_value]
    for value in values:
        slugs = tuple(
            slug
            for slug in (
                str(item or "").strip().lower()
                for item in str(value or "").split(",")
            )
            if slug
        )
        if slugs:
            groups.append(slugs)
    return tuple(groups)


def _resolve_tag_slug_ids(groups: tuple[tuple[str, ...], ...], context: dict) -> dict[str, int]:
    slugs = tuple(dict.fromkeys(
        slug
        for group in groups
        for slug in group
        if slug != "untagged"
    ))
    if not slugs:
        return {}

    resolved = resolve_runtime_model_slugs(
        Tag,
        slugs,
        context={"user": (context or {}).get("user")},
    )
    return {
        slug: int(tag.id)
        for slug, tag in resolved.items()
        if getattr(tag, "id", None)
    }


def _query_param_value(params, key: str) -> str:
    if not isinstance(params, dict):
        return ""
    value = params.get(key)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "").strip().lower()


def _has_value(value) -> bool:
    return bool(str(value or "").strip())
