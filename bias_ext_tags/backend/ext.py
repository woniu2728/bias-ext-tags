from bias_ext_tags.backend import tasks as tag_tasks  # noqa: F401
from bias_ext_tags.backend.extenders import (
    admin_extenders,
    event_extenders,
    forum_extenders,
    frontend_extenders,
    model_extenders,
    optional_integration_extenders,
    policy_extenders,
    resource_extenders,
    search_extenders,
    service_extenders,
)


def extend():
    return [
        *frontend_extenders(),
        *admin_extenders(),
        *forum_extenders(),
        *model_extenders(),
        *search_extenders(),
        *policy_extenders(),
        *resource_extenders(),
        *event_extenders(),
        *optional_integration_extenders(),
        *service_extenders(),
    ]
