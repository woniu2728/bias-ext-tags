from bias_core.extensions import DiscussionListQueryDefinition, PostTypeDefinition, SearchFilterDefinition

from bias_ext_tags.backend.constants import EXTENSION_ID
from bias_ext_tags.backend.search import (
    apply_discussion_tag_list_query,
    apply_discussion_tag_search_filter,
    hide_hidden_tag_discussions_from_all_list,
    parse_tag_search_filter,
)


def post_type_definitions():
    return (
        PostTypeDefinition(
            code="discussionTagged",
            label="讨论标签变更",
            module_id=EXTENSION_ID,
            description="记录讨论标签被调整的系统事件帖，不计入回复统计和全文搜索。",
            icon="fas fa-tags",
            is_default=False,
            is_stream_visible=True,
            counts_toward_discussion=False,
            counts_toward_user=False,
            searchable=False,
        ),
    )


def search_filter_definitions():
    return (
        SearchFilterDefinition(
            code="tag",
            label="按标签过滤",
            module_id=EXTENSION_ID,
            target="discussion",
            parser=parse_tag_search_filter,
            applier=apply_discussion_tag_search_filter,
            syntax="tag:<slug>",
            description="按标签 slug 过滤讨论搜索结果。",
        ),
    )


def discussion_list_query_definitions():
    return (
        DiscussionListQueryDefinition(
            key="hide-hidden-tags-from-all-discussions",
            module_id=EXTENSION_ID,
            applier=hide_hidden_tag_discussions_from_all_list,
            description="默认全部讨论列表隐藏归属隐藏标签的讨论。",
            order=30,
        ),
        DiscussionListQueryDefinition(
            key="tag",
            module_id=EXTENSION_ID,
            applier=apply_discussion_tag_list_query,
            description="按标签 slug 过滤讨论列表。",
            order=40,
        ),
    )
