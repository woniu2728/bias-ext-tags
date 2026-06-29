from __future__ import annotations

from typing import Iterable

from bias_ext_tags.backend.models import DiscussionTag


def get_discussion_tag_links(discussion):
    cached = getattr(discussion, "resource_discussion_tag_links_cache", None)
    if cached is not None:
        return list(cached)

    prefetched = getattr(discussion, "_prefetched_objects_cache", {})
    if "discussion_tags" in prefetched:
        links = list(prefetched["discussion_tags"])
        setattr(discussion, "resource_discussion_tag_links_cache", links)
        return links

    links = getattr(discussion, "discussion_tags", None)
    if links is not None:
        queryset = links.select_related("tag") if hasattr(links, "select_related") else links
        loaded = list(queryset.all() if hasattr(queryset, "all") else queryset)
        setattr(discussion, "resource_discussion_tag_links_cache", loaded)
        return loaded

    discussion_id = getattr(discussion, "id", discussion)
    if not discussion_id:
        return []
    loaded = list(
        DiscussionTag.objects.filter(discussion_id=discussion_id)
        .select_related("tag")
        .order_by("tag_id")
    )
    try:
        setattr(discussion, "resource_discussion_tag_links_cache", loaded)
    except Exception:
        pass
    return loaded


def get_discussion_tags(discussion) -> list:
    return [
        link.tag
        for link in get_discussion_tag_links(discussion)
        if getattr(link, "tag", None) is not None
    ]


def get_discussion_tag_ids(discussion) -> tuple[int, ...]:
    prefetched = getattr(discussion, "_prefetched_objects_cache", {})
    if "discussion_tags" in prefetched:
        return tuple(
            sorted(
                link.tag_id
                for link in prefetched["discussion_tags"]
                if getattr(link, "tag_id", None) is not None
            )
        )

    links = getattr(discussion, "discussion_tags", None)
    if links is not None and hasattr(links, "order_by"):
        return tuple(links.order_by("tag_id").values_list("tag_id", flat=True))

    discussion_id = getattr(discussion, "id", discussion)
    if not discussion_id:
        return ()
    return tuple(
        DiscussionTag.objects.filter(discussion_id=discussion_id)
        .order_by("tag_id")
        .values_list("tag_id", flat=True)
    )


def get_discussion_tag_names(discussion) -> tuple[str, ...]:
    return tuple(
        link.tag.name
        for link in sorted(
            get_discussion_tag_links(discussion),
            key=lambda item: getattr(getattr(item, "tag", None), "name", ""),
        )
        if getattr(link, "tag", None) is not None
    )


def serialize_discussion_tag_summaries(discussion) -> list[dict]:
    return [
        {
            "id": tag.id,
            "name": tag.name,
            "slug": tag.slug,
            "color": tag.color,
            "icon": tag.icon,
        }
        for tag in get_discussion_tags(discussion)
    ]


def replace_discussion_tags(discussion, tags: Iterable, *, previous_links: Iterable | None = None) -> dict:
    previous_links = tuple(previous_links) if previous_links is not None else tuple(get_discussion_tag_links(discussion))
    previous_tags = tuple(
        link.tag
        for link in previous_links
        if getattr(link, "tag", None) is not None
    )
    previous_tag_ids = tuple(sorted(
        tag.id
        for tag in previous_tags
        if getattr(tag, "id", None) is not None
    ))
    previous_tag_names = tuple(tag.name for tag in sorted(previous_tags, key=lambda item: item.name))
    normalized_tags = tuple(tags or ())
    current_tag_ids = tuple(
        tag.id
        for tag in normalized_tags
        if getattr(tag, "id", None) is not None
    )
    current_tag_names = tuple(tag.name for tag in sorted(normalized_tags, key=lambda item: item.name))
    previous_tag_id_set = set(previous_tag_ids)
    current_tag_id_set = set(current_tag_ids)

    if previous_tag_id_set == current_tag_id_set:
        return {
            "previous_tag_ids": previous_tag_ids,
            "previous_tag_names": previous_tag_names,
            "current_tag_ids": current_tag_ids,
            "current_tag_names": current_tag_names,
            "affected_tag_ids": (),
            "added_tag_ids": (),
            "removed_tag_ids": (),
            "added_tags": (),
            "removed_tags": (),
        }

    discussion_id = getattr(discussion, "id", discussion)
    if not discussion_id:
        raise ValueError("讨论对象缺少 id")

    DiscussionTag.objects.filter(discussion_id=discussion_id).delete()
    DiscussionTag.objects.bulk_create([
        DiscussionTag(discussion_id=discussion_id, tag=tag)
        for tag in normalized_tags
    ])
    _clear_discussion_tags_prefetch_cache(discussion)

    return {
        "previous_tag_ids": previous_tag_ids,
        "previous_tag_names": previous_tag_names,
        "current_tag_ids": current_tag_ids,
        "current_tag_names": current_tag_names,
        "affected_tag_ids": tuple(sorted(previous_tag_id_set | current_tag_id_set)),
        "added_tag_ids": tuple(sorted(current_tag_id_set - previous_tag_id_set)),
        "removed_tag_ids": tuple(sorted(previous_tag_id_set - current_tag_id_set)),
        "added_tags": tuple(name for name in current_tag_names if name not in previous_tag_names),
        "removed_tags": tuple(name for name in previous_tag_names if name not in current_tag_names),
    }


def tag_has_discussions(tag) -> bool:
    return DiscussionTag.objects.filter(tag=tag).exists()


def _clear_discussion_tags_prefetch_cache(discussion) -> None:
    prefetched = getattr(discussion, "_prefetched_objects_cache", None)
    if isinstance(prefetched, dict):
        prefetched.pop("discussion_tags", None)


def get_discussion_tag_ids_for_stats(discussion) -> list[int]:
    discussion_id = getattr(discussion, "id", discussion)
    if not discussion_id:
        return []
    return list(
        DiscussionTag.objects.filter(discussion_id=discussion_id)
        .values_list("tag_id", flat=True)
    )
