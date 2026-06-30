import { api } from '@bias/core'
import { normalizeTag, unwrapTagList } from './tagUtils.js'
import { buildTagTreeRequestOptions } from './tagTreeRequest.js'

const tagTreeState = {
  loadingPromise: null,
  tags: null,
}

export function resetTagTreeState() {
  tagTreeState.loadingPromise = null
  tagTreeState.tags = null
}

export async function loadTagTree({
  force = false,
  fetchTags = fetchTagTree,
} = {}) {
  if (!force && tagTreeState.tags) {
    return tagTreeState.tags
  }

  if (!force && tagTreeState.loadingPromise) {
    return tagTreeState.loadingPromise
  }

  const loadingPromise = fetchTags()
    .then(response => {
      const normalizedTags = unwrapTagList(response).map(normalizeTag)
      tagTreeState.tags = normalizedTags
      return normalizedTags
    })
    .finally(() => {
      if (tagTreeState.loadingPromise === loadingPromise) {
        tagTreeState.loadingPromise = null
      }
    })

  tagTreeState.loadingPromise = loadingPromise
  return loadingPromise
}

export function getCachedTagTree() {
  return tagTreeState.tags
}

function fetchTagTree() {
  return api.get('/tags', buildTagTreeRequestOptions())
}
