from bias_core.extensions import FrontendExtender


def frontend_extender():
    return (
        FrontendExtender(
            admin_entry="extensions/tags/frontend/admin/index.js",
            forum_entry="extensions/tags/frontend/forum/index.js",
        )
        .route(
            "/tags",
            "tags",
            "./TagsView.vue",
            title="全部标签",
            description="浏览论坛标签，按主题发现相关讨论。",
            preloads=(
                {
                    "href": "/api/tags?include_children=true",
                    "as": "fetch",
                    "crossorigin": "anonymous",
                },
            ),
            order=30,
        )
    )


def discussion_frontend_extender():
    return (
        FrontendExtender(
            forum_entry="extensions/tags/frontend/forum/index.js",
        )
        .route(
            "/t/:slug",
            "tag-detail",
            "extensions/discussions/frontend/forum/DiscussionListView.vue",
            title="标签讨论",
            description="查看该标签下的论坛讨论。",
            preloads=(
                {
                    "href": "/api/tags/slug/:slug",
                    "as": "fetch",
                    "crossorigin": "anonymous",
                },
                {
                    "href": "/api/tags?include_children=true",
                    "as": "fetch",
                    "crossorigin": "anonymous",
                },
            ),
            order=31,
        )
    )
