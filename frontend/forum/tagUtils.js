export function unwrapTagList(payload) {
  if (Array.isArray(payload?.data)) return payload.data
  if (Array.isArray(payload?.results)) return payload.results
  if (Array.isArray(payload)) return payload
  return []
}

export function normalizeTag(tag = {}) {
  return {
    ...tag,
    color: tag.color || '#6c7a89',
    is_primary: Boolean(tag.is_primary),
    is_child: Boolean(tag.is_child ?? tag.parent_id),
    children: unwrapTagList(tag.children).map(normalizeTag),
    last_posted_discussion: tag.last_posted_discussion || null,
  }
}

export function flattenTags(tags) {
  return unwrapTagList(tags).flatMap(tag => {
    const normalized = normalizeTag(tag)
    return [normalized, ...flattenTags(normalized.children)]
  })
}

export function buildTagPath(tagOrSlug) {
  const slug = typeof tagOrSlug === 'object' ? tagOrSlug?.slug : tagOrSlug
  return `/t/${slug}`
}

export function isPrimaryRootTag(tag = {}) {
  return Boolean(tag?.is_primary && !tag?.parent_id)
}

export function isChildTag(tag = {}) {
  return Boolean(tag?.parent_id || tag?.is_child)
}

export function isSecondaryRootTag(tag = {}) {
  return Boolean(!tag?.is_primary && !tag?.parent_id)
}

export function sortTagsByStructure(left = {}, right = {}) {
  const leftPosition = left.position === null || left.position === undefined ? Number.MAX_SAFE_INTEGER : Number(left.position)
  const rightPosition = right.position === null || right.position === undefined ? Number.MAX_SAFE_INTEGER : Number(right.position)
  if (leftPosition !== rightPosition) return leftPosition - rightPosition
  return String(left.name || '').localeCompare(String(right.name || ''), 'zh-CN')
}

export function groupTagsByStructure(tags = []) {
  const normalized = flattenTags(tags)
  const byId = new Map(normalized.map(tag => [tag.id, { ...tag, children: [] }]))

  for (const tag of normalized) {
    if (!isChildTag(tag)) continue
    const parent = byId.get(tag.parent_id)
    if (parent) {
      parent.children.push(byId.get(tag.id) || tag)
    }
  }

  const primaryTags = Array.from(byId.values())
    .filter(isPrimaryRootTag)
    .sort(sortTagsByStructure)
    .map(tag => ({
      ...tag,
      children: tag.children.slice().sort(sortTagsByStructure),
    }))
  const secondaryTags = Array.from(byId.values())
    .filter(isSecondaryRootTag)
    .sort((left, right) => String(left.name || '').localeCompare(String(right.name || ''), 'zh-CN'))
  const childTags = Array.from(byId.values())
    .filter(isChildTag)
    .sort(sortTagsByStructure)

  return {
    childTags,
    primaryTags,
    secondaryTags,
  }
}
