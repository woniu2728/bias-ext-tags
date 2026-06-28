import { ref, computed } from '@bias/core'
import { getTrackedDiscussionIdsFromDiscussionItems } from '@bias/realtime'
import { groupTagsByStructure, normalizeTag, unwrapTagList } from './tagUtils.js'

export function createTagsResourceState({
  resourceStore,
}) {
  const tagIds = ref([])

  const tags = computed(() => resourceStore.list('tags', tagIds.value))
  const tagGroups = computed(() => groupTagsByStructure(tags.value))
  const primaryTags = computed(() => tagGroups.value.primaryTags)
  const secondaryTags = computed(() => tagGroups.value.secondaryTags)
  const childTags = computed(() => tagGroups.value.childTags)
  const trackedDiscussionIds = computed(() => {
    return getTrackedDiscussionIdsFromDiscussionItems(
      tags.value
        .map(tag => tag?.last_posted_discussion)
        .filter(Boolean)
    )
  })
  const cloudTags = computed(() => secondaryTags.value.slice(0, 12))

  function applyTagsResponse(response) {
    tagIds.value = resourceStore.upsertMany('tags', unwrapTagList(response).map(normalizeTag))
      .map(item => item.id)
  }

  function resetTags() {
    tagIds.value = []
  }

  return {
    applyTagsResponse,
    cloudTags,
    childTags,
    primaryTags,
    resetTags,
    secondaryTags,
    tagIds,
    tags,
    tagGroups,
    trackedDiscussionIds,
  }
}

export function useTagsResourceState({
  resourceStore,
}) {
  return createTagsResourceState({
    resourceStore,
  })
}
