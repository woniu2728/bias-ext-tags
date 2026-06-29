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

  return {
    availableTagCount: flatTags.filter(tag => isPrimaryRootTag(tag) || isSecondaryRootTag(tag)).length,
    flatTags,
    limits,
    primaryTags: flatTags.filter(isPrimaryRootTag).sort(sortTagsByStructure),
    rootSecondaryTags: flatTags.filter(isSecondaryRootTag).sort(sortTagsByStructure),
    secondaryOptions,
    selectedPrimaryIds,
    selectedSecondaryIds,
    selectedTagIds: [...selectedPrimaryIds, ...selectedSecondaryIds],
    selectedTags: [...selectedPrimaryIds, ...selectedSecondaryIds].map(tagId => byId.get(tagId)).filter(Boolean),
  }
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
