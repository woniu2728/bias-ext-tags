import {
  flattenTags,
  isChildTag,
  isPrimaryRootTag,
  isSecondaryRootTag,
  sortTagsByStructure,
  unwrapTagList,
} from './tagUtils.js'

export function parseTagLimit(value, fallback = 0) {
  const parsed = Number.parseInt(value, 10)
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback
}

export function resolveTagSelectionLimits(settings = {}) {
  return {
    minPrimary: parseTagLimit(settings.min_primary_tags, 0),
    maxPrimary: parseTagLimit(settings.max_primary_tags, 1),
    minSecondary: parseTagLimit(settings.min_secondary_tags, 0),
    maxSecondary: parseTagLimit(settings.max_secondary_tags, 1),
  }
}

export function normalizeSelectionIds(values = []) {
  return Array.from(new Set(
    unwrapTagList(values)
      .map(value => Number.parseInt(value, 10))
      .filter(Number.isInteger)
  ))
}

export function createTagSelectionState({
  tags = [],
  primaryTagIds = [],
  secondaryTagIds = [],
  settings = {},
} = {}) {
  const flatTags = flattenTags(tags)
  const byId = new Map(flatTags.map(tag => [Number(tag.id), tag]))
  const limits = resolveTagSelectionLimits(settings)
  const selectedPrimaryIds = normalizeSelectionIds(primaryTagIds)
    .filter(tagId => isPrimaryRootTag(byId.get(tagId)))
    .slice(0, limits.maxPrimary)
  const primaryIdSet = new Set(selectedPrimaryIds)
  const secondaryOptions = flatTags
    .filter(tag => {
      if (isSecondaryRootTag(tag)) return true
      return isChildTag(tag) && primaryIdSet.has(Number(tag.parent_id))
    })
    .sort(sortTagsByStructure)
  const allowedSecondaryIds = new Set(secondaryOptions.map(tag => Number(tag.id)))
  const selectedSecondaryIds = normalizeSelectionIds(secondaryTagIds)
    .filter(tagId => allowedSecondaryIds.has(tagId))
    .slice(0, limits.maxSecondary)
  const primaryTags = flatTags.filter(isPrimaryRootTag).sort(sortTagsByStructure)
  const rootSecondaryTags = flatTags.filter(isSecondaryRootTag).sort(sortTagsByStructure)
  const selectedTagIds = [...selectedPrimaryIds, ...selectedSecondaryIds]
  const requirement = resolveTagSelectionRequirement({
    availableTagCount: primaryTags.length + secondaryOptions.length,
    bypassTagCounts: settings.can_bypass_tag_counts,
    limits,
    selectedPrimaryCount: selectedPrimaryIds.length,
    selectedSecondaryCount: selectedSecondaryIds.length,
  })

  return {
    availableTagCount: primaryTags.length + secondaryOptions.length,
    flatTags,
    limits,
    primaryTags,
    rootSecondaryTags,
    secondaryOptions,
    requirement,
    selectedPrimaryIds,
    selectedPrimaryCount: selectedPrimaryIds.length,
    selectedSecondaryIds,
    selectedSecondaryCount: selectedSecondaryIds.length,
    selectedTagCount: selectedTagIds.length,
    selectedTagIds,
    selectedTags: selectedTagIds.map(tagId => byId.get(tagId)).filter(Boolean),
  }
}

export function resolveTagSelectionRequirement({
  availableTagCount = 0,
  bypassTagCounts = false,
  limits = {},
  selectedPrimaryCount = 0,
  selectedSecondaryCount = 0,
} = {}) {
  if (bypassTagCounts) return null
  if (Number(availableTagCount || 0) <= 0) {
    return {
      code: 'unavailable',
      message: '当前没有可用标签，暂时无法发布讨论。',
    }
  }

  const normalizedLimits = {
    minPrimary: parseTagLimit(limits.minPrimary, 0),
    maxPrimary: parseTagLimit(limits.maxPrimary, 1),
    minSecondary: parseTagLimit(limits.minSecondary, 0),
    maxSecondary: parseTagLimit(limits.maxSecondary, 1),
  }
  const primaryCount = Number(selectedPrimaryCount || 0)
  const secondaryCount = Number(selectedSecondaryCount || 0)

  if (primaryCount < normalizedLimits.minPrimary) {
    return {
      code: 'min_primary',
      message: `当前至少需要选择 ${normalizedLimits.minPrimary} 个主标签。`,
    }
  }
  if (primaryCount > normalizedLimits.maxPrimary) {
    return {
      code: 'max_primary',
      message: `当前最多只能选择 ${normalizedLimits.maxPrimary} 个主标签。`,
    }
  }
  if (secondaryCount < normalizedLimits.minSecondary) {
    return {
      code: 'min_secondary',
      message: `当前至少需要选择 ${normalizedLimits.minSecondary} 个次标签。`,
    }
  }
  if (secondaryCount > normalizedLimits.maxSecondary) {
    return {
      code: 'max_secondary',
      message: `当前最多只能选择 ${normalizedLimits.maxSecondary} 个次标签。`,
    }
  }
  if (primaryCount + secondaryCount <= 0) {
    return {
      code: 'empty',
      message: '请选择标签后再发布讨论。',
    }
  }

  return null
}

export function resolveRequestedSelection(tagId, tags = []) {
  const requestedId = Number.parseInt(tagId, 10)
  if (!Number.isInteger(requestedId)) {
    return { primaryTagIds: [], secondaryTagIds: [] }
  }
  const requestedTag = flattenTags(tags).find(tag => Number(tag.id) === requestedId)
  if (!requestedTag) {
    return { primaryTagIds: [], secondaryTagIds: [] }
  }
  if (isChildTag(requestedTag)) {
    return {
      primaryTagIds: normalizeSelectionIds([requestedTag.parent_id]),
      secondaryTagIds: [Number(requestedTag.id)],
    }
  }
  if (isSecondaryRootTag(requestedTag)) {
    return {
      primaryTagIds: [],
      secondaryTagIds: [Number(requestedTag.id)],
    }
  }
  if (isPrimaryRootTag(requestedTag)) {
    return {
      primaryTagIds: [Number(requestedTag.id)],
      secondaryTagIds: [],
    }
  }
  return { primaryTagIds: [], secondaryTagIds: [] }
}

export function summarizeSelectedTags(selectedTags = []) {
  return unwrapTagList(selectedTags).map(tag => tag?.name).filter(Boolean).join(' / ')
}
