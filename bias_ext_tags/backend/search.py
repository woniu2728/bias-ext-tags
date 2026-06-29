from __future__ import annotations

from django.db.models import Exists, OuterRef, Q, Subquery

from bias_ext_tags.backend.models import DiscussionTag, Tag


def parse_tag_search_filter(token: str) -> dict | str | None:
    if not token or ":" not in token:
        return None

    prefix, value = token.split(":", 1)
    negate = prefix.startswith("-")
    if negate:
        prefix = prefix[1:]
    if prefix.lower() != "tag":
        return None

    normalized = value.strip().lower()
    if not normalized:
        return None
    return {"value": normalized, "negate": negate} if negate else normalized


def apply_discussion_tag_search_filter(queryset, tag_slug: str, context: dict):
    return _apply_discussion_tag_filter(queryset, tag_slug, context)


def apply_post_tag_search_filter(queryset, tag_slug: str, context: dict):
    return _apply_post_tag_filter(queryset, tag_slug, context)


def apply_discussion_tag_list_query(queryset, context: dict):
    tag_slug = _tag_query_param_value((context or {}).get("params"))
    if not tag_slug:
        return queryset
    return _apply_discussion_tag_filter(queryset, tag_slug, context)


def hide_hidden_tag_discussions_from_all_list(queryset, context: dict):
    context = context or {}
    params = context.get("params")
    if (
        _has_active_filters(context.get("active_filters"))
        or _has_value(context.get("query"))
        or _has_value(context.get("author"))
        or _tag_query_param_value(params)
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
    matching_tag_ids = Tag.objects.filter(
        Q(name__istartswith=value) | Q(slug__istartswith=value),
    ).values("id")
    return state.filter(id__in=Subquery(matching_tag_ids))


def search_tags(queryset, criteria, context: dict):
    from bias_ext_tags.backend.services import TagService

    return queryset.order_by(*TagService.structure_order_by(include_id=True))


def _apply_discussion_tag_filter(queryset, raw_value: str, context: dict):
    return _apply_tag_filter(queryset, raw_value, context, discussion_ref="pk")


def _apply_post_tag_filter(queryset, raw_value: str, context: dict):
    return _apply_tag_filter(queryset, raw_value, context, discussion_ref="discussion_id")


def _apply_tag_filter(queryset, raw_value: str, context: dict, *, discussion_ref: str):
    filter_groups = _tag_filter_groups(raw_value)
    groups = tuple(group for group, _negate in filter_groups)
    if not groups:
        return queryset

    slug_to_id = _resolve_tag_slug_ids(groups, context)
    output = queryset
    for group, negate in filter_groups:
        condition = _discussion_tag_group_condition(group, slug_to_id, discussion_ref)

        if not condition:
            if negate:
                continue
            return queryset.none()
        output = output.exclude(condition) if negate else output.filter(condition)
    return output


def _tag_filter_groups(raw_value) -> tuple[tuple[tuple[str, ...], bool], ...]:
    groups: list[tuple[tuple[str, ...], bool]] = []
    values = raw_value if isinstance(raw_value, (list, tuple)) else [raw_value]
    for value in values:
        negate = False
        raw_group = value
        if isinstance(value, dict):
            raw_group = value.get("value")
            negate = bool(value.get("negate"))
        for group in _tag_slug_groups(raw_group):
            groups.append((group, negate))
    return tuple(groups)


def _discussion_tag_group_condition(group: tuple[str, ...], slug_to_id: dict[str, int], discussion_ref: str):
    discussion_tags = DiscussionTag.objects.filter(discussion_id=OuterRef(discussion_ref))
    condition = Q()

    for slug in group:
        if slug == "untagged":
            condition |= ~Exists(discussion_tags)
            continue

        tag_id = slug_to_id.get(slug)
        if tag_id is None:
            continue
        condition |= Exists(discussion_tags.filter(tag_id=tag_id))

    return condition


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

    cache = None
    if isinstance(context, dict):
        cache = context.setdefault("_tag_slug_ids_cache", {})
        missing_slugs = tuple(slug for slug in slugs if slug not in cache)
    else:
        missing_slugs = slugs

    if cache is not None and not missing_slugs:
        return {
            slug: int(cache[slug])
            for slug in slugs
            if cache.get(slug) is not None
        }

    from bias_core.extensions.runtime import resolve_runtime_model_slugs

    resolved = resolve_runtime_model_slugs(
        Tag,
        missing_slugs,
        identifier=None,
        context={"user": (context or {}).get("user")},
    )
    resolved_ids = {
        slug: int(tag.id)
        for slug, tag in resolved.items()
        if getattr(tag, "id", None)
    }
    if cache is not None:
        for slug in missing_slugs:
            cache[slug] = resolved_ids.get(slug)
        return {
            slug: int(cache[slug])
            for slug in slugs
            if cache.get(slug) is not None
        }
    return resolved_ids


def _query_param_value(params, key: str):
    if not isinstance(params, dict):
        return ""
    value = params.get(key)
    if isinstance(value, (list, tuple)):
        return [
            str(item or "").strip().lower()
            for item in value
            if str(item or "").strip()
        ]
    return str(value or "").strip().lower()


def _tag_query_param_value(params):
    return _query_param_value(params, "tag") or _query_param_value(params, "filter[tag]")


def _has_value(value) -> bool:
    return bool(str(value or "").strip())


def _has_active_filters(value) -> bool:
    if isinstance(value, dict):
        return any(_has_value(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_has_value(item) for item in value)
    return _has_value(value)
