import { getUiCopy } from '@bias/core/forum'
import { renderTwemojiHtml } from '@bias/emoji'
import { highlightSearchText } from '@bias/search'
import { buildTagPath } from './tagUtils.js'

export function createTagSearchSource() {
  return {
    key: 'tags',
    moduleId: 'tags',
    type: 'tag',
    routeType: 'tag',
    apiType: 'tag',
    resourceType: 'tags',
    resultsKey: 'tags',
    totalKey: 'tag_total',
    label: getUiCopy({
      surface: 'search-section-tags-title',
    })?.text || '标签',
    filterTarget: 'tag',
    icon: 'fas fa-tags',
    order: 35,
    buildResultItems(items, { query } = {}) {
      return items.map(tag => {
        const discussionCount = Number(tag.discussion_count || 0)
        const countText = getUiCopy({
          surface: 'search-tag-result-discussions',
          count: discussionCount,
        })?.text || `${discussionCount} 条讨论`

        return {
          key: `tag-${tag.id}`,
          excerptHtml: renderTwemojiHtml(highlightSearchText(tag.description || countText, query, 160)),
          iconClass: tag.icon || 'fas fa-tags',
          metaItems: [
            `/${tag.slug}`,
            countText,
          ],
          path: buildTagPath(tag),
          titleHtml: renderTwemojiHtml(highlightSearchText(tag.name || tag.slug || '标签', query, 80)),
          titleText: tag.name || tag.slug || '标签',
          userLayout: false,
        }
      })
    },
  }
}
