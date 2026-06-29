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
    return [normalized, ...normalized.children]
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
  const leftSecondary = left.position === null || left.position === undefined
  const rightSecondary = right.position === null || right.position === undefined
  if (leftSecondary && rightSecondary) {
    return Number(right.discussion_count || 0) - Number(left.discussion_count || 0)
  }
  if (rightSecondary) return -1
  if (leftSecondary) return 1

  const leftPosition = left.position === null || left.position === undefined ? Number.MAX_SAFE_INTEGER : Number(left.position)
  const rightPosition = right.position === null || right.position === undefined ? Number.MAX_SAFE_INTEGER : Number(right.position)

  const leftParent = left.parent || null
  const rightParent = right.parent || null
  const leftParentId = left.parent_id ?? leftParent?.id ?? null
  const rightParentId = right.parent_id ?? rightParent?.id ?? null

  if (leftParentId || rightParentId) {
    if (leftParentId && rightParentId && String(leftParentId) === String(rightParentId)) {
      return leftPosition - rightPosition
    }

    const leftParentPosition = normalizeSortablePosition(leftParent?.position)
    const rightParentPosition = normalizeSortablePosition(rightParent?.position)

    if (leftParentId && rightParentId) {
      const parentDelta = leftParentPosition - rightParentPosition
      return parentDelta !== 0 ? parentDelta : leftPosition - rightPosition
    }

    if (leftParentId) {
      if (String(leftParentId) === String(right.id)) return 1
      const parentDelta = leftParentPosition - rightPosition
      return parentDelta !== 0 ? parentDelta : 1
    }

    if (rightParentId) {
      if (String(rightParentId) === String(left.id)) return -1
      const parentDelta = leftPosition - rightParentPosition
      return parentDelta !== 0 ? parentDelta : -1
    }
  }

  if (leftPosition !== rightPosition) return leftPosition - rightPosition
  return String(left.name || '').localeCompare(String(right.name || ''), 'zh-CN')
}

export function sortTags(tags = []) {
  const normalizedTags = unwrapTagList(tags).map(normalizeTag)
  const byId = new Map(normalizedTags.map(tag => [Number(tag.id), tag]))
  return normalizedTags
    .map(tag => tag.parent_id && !tag.parent ? {
      ...tag,
      parent: byId.get(Number(tag.parent_id)) || null,
    } : tag)
    .sort(sortTagsByStructure)
}

function normalizeSortablePosition(position) {
  return position === null || position === undefined ? Number.MAX_SAFE_INTEGER : Number(position)
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
    .sort(sortTagsByStructure)
  const childTags = Array.from(byId.values())
    .filter(isChildTag)
    .sort(sortTagsByStructure)

  return {
    childTags,
    primaryTags,
    secondaryTags,
  }
}
