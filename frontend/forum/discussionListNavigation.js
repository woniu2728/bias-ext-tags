import { isChildTag, isPrimaryRootTag, isSecondaryRootTag, sortTags } from './tagUtils.js'

export function sortDiscussionListSidebarTags(normalizedTags = []) {
  return sortTags(normalizedTags)
}

export function findDiscussionListSidebarContextParent(targetSlug, normalizedTags = []) {
  if (!targetSlug) return null

  for (const tag of normalizedTags) {
    if (tag.slug === targetSlug) return tag

    const children = Array.isArray(tag.children) ? tag.children : []
    if (children.some(child => child.slug === targetSlug)) {
      return tag
    }
  }

  return null
}

export function buildDiscussionListPrimaryTagItems(flatTags = [], contextParent = null) {
  return sortDiscussionListSidebarTags(flatTags).filter(tag => {
    if (isPrimaryRootTag(tag)) return true
    return Boolean(contextParent && isChildTag(tag) && tag.parent_id === contextParent.id)
  })
}

export function buildDiscussionListSecondaryTagItems(flatTags = []) {
  return sortDiscussionListSidebarTags(flatTags)
    .filter(isSecondaryRootTag)
    .slice(0, 3)
}

export function isDiscussionSidebarTagActive({
  currentTag,
  currentTagSlug,
  normalizedTags,
  tag,
}) {
  if (currentTagSlug === tag.slug) return true

  const parent = findDiscussionListSidebarContextParent(currentTagSlug, normalizedTags)
  return Boolean(currentTag?.parent_id && parent?.slug === tag.slug)
}
