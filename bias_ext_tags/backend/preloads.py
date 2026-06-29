from __future__ import annotations

from bias_core.extensions.platform import ResourceQueryOptions, apply_resource_preloads
from bias_ext_tags.backend.services import TagService


def get_runtime_resource_registry(*args, **kwargs):
    from bias_core.extensions.runtime import get_runtime_resource_registry as runtime_get_resource_registry

    return runtime_get_resource_registry(*args, **kwargs)


def get_resource_registry():
    return get_runtime_resource_registry()


def apply_tag_resource_preloads(queryset, user=None, action="view", resource_options=None):
    resource_options = resource_options or ResourceQueryOptions()
    queryset = TagService.prefetch_state_for_user(queryset, user)
    return apply_resource_preloads(
        get_resource_registry(),
        queryset,
        "tag",
        context={"user": user, "action": action},
        resource_options=resource_options,
    )
