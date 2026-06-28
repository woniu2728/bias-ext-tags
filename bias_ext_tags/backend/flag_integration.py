from bias_core.extensions import ApiResourceExtender


def flag_resource_extenders():
    return [
        ApiResourceExtender("flag").eager_load_when_included(
            "index",
            "post",
            "post__discussion__discussion_tags__tag",
        ),
    ]
