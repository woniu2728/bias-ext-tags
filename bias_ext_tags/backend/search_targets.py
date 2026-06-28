from __future__ import annotations


def tag_search_target_provider() -> dict:
    from bias_ext_tags.backend.models import Tag
    from bias_ext_tags.backend.services import TagService

    return {
        "model": Tag,
        "resource": "tag",
        "results_key": "tags",
        "apply_visibility": lambda queryset, user: TagService.filter_tags_for_user(queryset, user, action="view"),
        "order_by": ("position", "name", "id"),
    }
