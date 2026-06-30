from bias_core.extensions import (
    ExtensionModelDefinition,
    ExtensionModelRelationDefinition,
    ExtensionModelVisibilityDefinition,
)

from bias_ext_tags.backend.models import DiscussionTag, Tag
from bias_ext_tags.backend.resources import resolve_post_event_mentions_tags
from bias_ext_tags.backend.runtime_models import DISCUSSION_MODEL, POST_MODEL
from bias_ext_tags.backend.services import TagService
from bias_ext_tags.backend.tag_relationships import get_discussion_tags


def model_definitions():
    return (
        ExtensionModelDefinition(
            model=DISCUSSION_MODEL,
            key="tags",
            handler=DiscussionTag,
            kind="manyToMany",
            description="讨论关联标签。",
        ),
    )


def post_model_definitions():
    return (
        ExtensionModelDefinition(
            model=POST_MODEL,
            key="eventPostMentionsTags",
            handler=Tag,
            kind="relationship",
            description="标签变更事件帖中涉及的标签。",
        ),
    )


def model_relation_definitions():
    return (
        ExtensionModelRelationDefinition(
            model=DISCUSSION_MODEL,
            name="tags",
            resolver=get_discussion_tags,
            relation_type="belongsToMany",
            related_model=Tag,
            description="讨论关联标签。",
            inject_attribute=False,
        ),
        ExtensionModelRelationDefinition(
            model=Tag,
            name="parent",
            resolver=lambda tag: tag.parent,
            relation_type="belongsTo",
            related_model=Tag,
            description="标签父级。",
        ),
        ExtensionModelRelationDefinition(
            model=Tag,
            name="children",
            resolver=lambda tag: tag.children.all(),
            relation_type="hasMany",
            related_model=Tag,
            description="标签子级。",
        ),
    )


def post_model_relation_definitions():
    return (
        ExtensionModelRelationDefinition(
            model=POST_MODEL,
            name="eventPostMentionsTags",
            resolver=resolve_post_event_mentions_tags,
            relation_type="relationship",
            related_model=Tag,
            description="标签变更事件帖中涉及的标签。",
        ),
    )


def model_visibility_definitions():
    return (
        ExtensionModelVisibilityDefinition(
            model=DISCUSSION_MODEL,
            ability="*",
            scope=lambda queryset, context: TagService.filter_discussions_for_user(
                queryset,
                context.get("user"),
                ability=context.get("ability") or "view",
                context=context,
            ),
            description="按当前能力隐藏用户在标签权限下不可访问的讨论。",
        ),
        ExtensionModelVisibilityDefinition(
            model=Tag,
            ability="view",
            scope=lambda queryset, context: TagService.filter_tags_for_user(
                queryset,
                context.get("user"),
                action="view",
            ),
            description="隐藏当前用户不可查看的标签。",
        ),
    )


def post_model_visibility_definitions():
    return (
        ExtensionModelVisibilityDefinition(
            model=POST_MODEL,
            ability="view",
            scope=lambda queryset, context: TagService.filter_posts_for_user(
                queryset,
                context.get("user"),
            ),
            description="隐藏当前用户不可查看标签下的帖子。",
        ),
    )
