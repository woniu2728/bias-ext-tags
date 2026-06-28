from bias_core.extensions import (
    AdminSurfaceExtender,
    ApiResourceExtender,
    ApiRoutesExtender,
    ConsoleExtender,
    ConditionalExtender,
    DiscussionLifecycleExtender,
    EventListenersExtender,
    ForumCapabilitiesExtender,
    LifecycleExtender,
    ModelExtender,
    ModelUrlExtender,
    ModelVisibilityExtender,
    PolicyExtender,
    PostLifecycleExtender,
    PostEventExtender,
    RealtimeExtender,
    SearchDriverExtender,
    ServiceProviderExtender,
    SettingsExtender,
)

from bias_ext_tags.backend.admin_api import router as admin_tags_router
from bias_ext_tags.backend.admin_surface import admin_page_definitions, permission_definitions
from bias_ext_tags.backend.console import refresh_tag_stats_console_command
from bias_ext_tags.backend.discussion_lifecycle import (
    apply_discussion_create,
    apply_discussion_approved,
    apply_discussion_delete,
    apply_discussion_hidden,
    apply_discussion_rejected,
    apply_discussion_update,
    prepare_discussion_delete,
)
from bias_ext_tags.backend.events import DiscussionTaggedEvent
from bias_ext_tags.backend.flag_integration import flag_resource_extenders
from bias_ext_tags.backend.forum_contracts import (
    discussion_list_query_definitions,
    post_type_definitions,
    search_filter_definitions,
)
from bias_ext_tags.backend.frontend import frontend_extender
from bias_ext_tags.backend.listeners import (
    enrich_realtime_tags_included_payload,
    post_event_listener_definitions,
    tag_event_listener_definitions,
)
from bias_ext_tags.backend.model_contracts import (
    model_definitions,
    model_relation_definitions,
    model_visibility_definitions,
    post_model_definitions,
    post_model_relation_definitions,
    post_model_visibility_definitions,
)
from bias_ext_tags.backend.models import DiscussionTag, Tag, TagState
from bias_ext_tags.backend.policies import DiscussionPolicy, PostPolicy, TagPolicy
from bias_ext_tags.backend.post_lifecycle import (
    apply_post_approved,
    apply_post_created,
    apply_post_deleted,
    apply_post_hidden,
    prepare_post_delete,
)
from bias_ext_tags.backend.resources import (
    discussion_resource_field_definitions,
    discussion_resource_relationship_definitions,
    forum_resource_field_definitions,
    forum_resource_relationship_definitions,
    resolve_discussion_tagged_event_data,
    tag_resource_definition,
    tag_resource_endpoints,
    tag_resource_field_definitions,
    tag_resource_relationship_definitions,
)
from bias_ext_tags.backend.runtime import tag_service_provider
from bias_ext_tags.backend.runtime_models import DISCUSSION_MODEL, POST_MODEL
from bias_ext_tags.backend.search_contracts import search_driver_definitions
from bias_ext_tags.backend.search_targets import tag_search_target_provider
from bias_ext_tags.backend.settings import setting_field_definitions
from bias_ext_tags.backend.slug import TagIdWithSlugDriver, TagSlugDriver


def frontend_extenders():
    return (frontend_extender(),)


def admin_extenders():
    return (
        AdminSurfaceExtender(
            permissions=permission_definitions(),
            admin_pages=admin_page_definitions(),
        ),
        ConsoleExtender().command(
            "tags.refresh_stats",
            refresh_tag_stats_console_command,
            description="重新计算标签讨论数和最后发帖讨论。",
        ),
        ApiRoutesExtender(
            mounts=(("/admin", admin_tags_router),),
            tags=("Admin",),
        ),
        SettingsExtender(
            fields=setting_field_definitions(),
            expose_to_forum=(
                "min_primary_tags",
                "max_primary_tags",
                "min_secondary_tags",
                "max_secondary_tags",
            ),
        ),
    )


def forum_extenders():
    return (
        ForumCapabilitiesExtender(
            discussion_list_queries=discussion_list_query_definitions(),
            search_filters=search_filter_definitions(),
        ),
    )


def model_extenders():
    return (
        ModelExtender(
            definitions=model_definitions(),
            relations=model_relation_definitions(),
        ).owns(
            Tag,
            description="标签模型由 tags 扩展拥有。",
        ).owns(
            DiscussionTag,
            description="讨论标签关系由 tags 扩展拥有。",
        ).owns(
            TagState,
            description="用户标签状态由 tags 扩展拥有。",
        ),
        ModelUrlExtender(Tag).add_slug_driver(
            "default",
            TagSlugDriver,
            field="slug",
            source_field="name",
            max_length=100,
            description="标签 URL slug 生成器。",
        ).add_slug_driver(
            "id_with_slug",
            TagIdWithSlugDriver,
            field="slug",
            source_field="name",
            max_length=100,
            description="标签 ID + slug URL 生成器。",
        ),
        ModelVisibilityExtender(
            definitions=model_visibility_definitions(),
        ),
    )


def search_extenders():
    return (
        SearchDriverExtender(
            drivers=search_driver_definitions(),
        ),
    )


def policy_extenders():
    return (
        PolicyExtender()
        .policy(DISCUSSION_MODEL, DiscussionPolicy)
        .policy(Tag, TagPolicy),
    )


def resource_extenders():
    return (
        ApiResourceExtender("discussion")
        .fields(discussion_resource_field_definitions)
        .relationships(discussion_resource_relationship_definitions)
        .add_default_include(("index", "show", "create"), ("tags",)),
        ApiResourceExtender("forum")
        .fields(forum_resource_field_definitions)
        .relationships(forum_resource_relationship_definitions),
        ApiResourceExtender(tag_resource_definition())
        .fields(tag_resource_field_definitions)
        .relationships(tag_resource_relationship_definitions)
        .endpoints(tag_resource_endpoints),
        ConditionalExtender().when_extension_enabled("flags", flag_resource_extenders),
    )


def event_extenders():
    return (
        EventListenersExtender(
            listeners=tag_event_listener_definitions(),
        ),
        DiscussionLifecycleExtender().handler(
            "tags",
            apply_create=apply_discussion_create,
            apply_update=apply_discussion_update,
            prepare_delete=prepare_discussion_delete,
            apply_delete=apply_discussion_delete,
            apply_hidden=apply_discussion_hidden,
            apply_approved=apply_discussion_approved,
            apply_rejected=apply_discussion_rejected,
            description="讨论创建、编辑、删除和状态变化时维护标签关系与统计刷新。",
        ),
        RealtimeExtender().included_payload(
            "tags",
            enrich_realtime_tags_included_payload,
            description="实时讨论事件 payload 中补充讨论关联标签。",
        ),
    )


def optional_integration_extenders():
    return (
        ConditionalExtender().when_extension_enabled("posts", post_integration_extenders),
    )


def service_extenders():
    return (
        ServiceProviderExtender(
            key="tags.service",
            provider=tag_service_provider,
        ),
        ServiceProviderExtender(
            key="search.target.tag",
            provider=tag_search_target_provider,
        ),
        LifecycleExtender(),
    )


def post_integration_extenders():
    return (
        ForumCapabilitiesExtender(
            post_types=post_type_definitions(),
        ),
        PostEventExtender().type(
            "discussionTagged",
            resolve_discussion_tagged_event_data,
            description="标签变更事件帖的结构化 payload。",
        ),
        ModelExtender(
            definitions=post_model_definitions(),
            relations=post_model_relation_definitions(),
        ),
        ModelVisibilityExtender(
            definitions=post_model_visibility_definitions(),
        ),
        PolicyExtender().policy(POST_MODEL, PostPolicy),
        ApiResourceExtender("post")
        .model_relationship(
            "eventPostMentionsTags",
            resource_type="tag",
            many=True,
            description="标签变更事件帖中涉及的标签关系。",
        )
        .add_default_include(("index",), ("eventPostMentionsTags",)),
        EventListenersExtender(
            listeners=post_event_listener_definitions(),
        ),
        PostLifecycleExtender().handler(
            "tags",
            apply_created=apply_post_created,
            apply_approved=apply_post_approved,
            apply_hidden=apply_post_hidden,
            prepare_delete=prepare_post_delete,
            apply_deleted=apply_post_deleted,
            description="回复状态变化后增量维护关联标签最后活跃讨论。",
        ),
        RealtimeExtender().broadcast_discussion_event(
            DiscussionTaggedEvent,
            "discussion.tagged",
            include_discussion=True,
            extension_context=lambda event: {"tags": {"tag_ids": list(event.tag_ids)}} if event.tag_ids else None,
            description="讨论标签变更后向讨论实时流广播标签状态变更。",
        ),
    )
