from bias_core.extensions import ExtensionSearchDriverDefinition

from bias_ext_tags.backend.forum_contracts import search_filter_definitions
from bias_ext_tags.backend.models import Tag
from bias_ext_tags.backend.search import apply_tag_fulltext_search, search_tags


def search_driver_definitions():
    return (
        ExtensionSearchDriverDefinition(
            target="discussion",
            driver="database",
            filters=search_filter_definitions(),
            description="按标签过滤讨论搜索。",
        ),
        ExtensionSearchDriverDefinition(
            target="tag",
            driver="database",
            model=Tag,
            searcher=search_tags,
            fulltext=apply_tag_fulltext_search,
            description="按名称或 slug 搜索标签。",
        ),
    )
