<template>
  <div class="composer-tag-fields">
    <select
      class="composer-field composer-tag-select"
      :multiple="tagLimits.maxPrimary > 1"
      :size="primarySelectSize"
      :disabled="submitting || loadingTags || isSuspended || tagLimits.maxPrimary <= 0"
      @change="handlePrimaryTagChange"
      @keydown.esc.prevent="closeComposer"
    >
      <option value="">{{ primaryTagPlaceholderText }}</option>
      <option
        v-for="tag in primaryTags"
        :key="tag.id"
        :selected="primaryTagIds.includes(String(tag.id))"
        :value="String(tag.id)"
      >
        {{ tag.name }}
      </option>
    </select>
    <select
      class="composer-field composer-tag-select"
      :multiple="tagLimits.maxSecondary > 1"
      :size="secondarySelectSize"
      :disabled="submitting || loadingTags || isSuspended || tagLimits.maxSecondary <= 0 || !secondaryTagOptions.length"
      @change="handleSecondaryTagChange"
      @keydown.esc.prevent="closeComposer"
    >
      <option value="">{{ secondaryTagPlaceholderText }}</option>
      <option
        v-for="tag in secondaryTagOptions"
        :key="tag.id"
        :selected="secondaryTagIds.includes(String(tag.id))"
        :value="String(tag.id)"
      >
        {{ tag.name }}
      </option>
    </select>
  </div>
</template>

<script setup>

import {
  api,
  watch,
  computed,
  ref } from '@bias/core'
import {
  getUiCopy,
  useForumStore,
} from '@bias/core/forum'
import {
  normalizeTag,
  unwrapTagList,
} from './tagUtils.js'
import {
  createTagSelectionState,
  normalizeSelectionIds,
  resolveRequestedSelection,
  resolveTagSelectionLimits,
  summarizeSelectedTags,
} from './tagSelectionState.js'

const props = defineProps({
  closeComposer: {
    type: Function,
    default: () => {}
  },
  current: {
    type: Object,
    default: () => ({})
  },
  isSuspended: {
    type: Boolean,
    default: false
  },
  requestId: {
    type: [Number, String],
    default: 0
  },
  state: {
    type: Object,
    default: () => ({})
  },
  submitting: {
    type: Boolean,
    default: false
  },
  updateState: {
    type: Function,
    default: () => {}
  }
})

const tags = ref([])
const loadingTags = ref(false)
const primaryTagIds = ref([])
const secondaryTagIds = ref([])
const forumStore = useForumStore()

const tagLimits = computed(() => resolveTagSelectionLimits(forumStore.settings || {}))
const selectionState = computed(() => createTagSelectionState({
  tags: tags.value,
  primaryTagIds: primaryTagIds.value,
  secondaryTagIds: secondaryTagIds.value,
  settings: forumStore.settings || {},
}))
const primaryTags = computed(() => selectionState.value.primaryTags)
const rootSecondaryTags = computed(() => selectionState.value.rootSecondaryTags)
const secondaryTagOptions = computed(() => selectionState.value.secondaryOptions)
const selectedTagIds = computed(() => selectionState.value.selectedTagIds)
const selectedTagLabel = computed(() => summarizeSelectedTags(selectionState.value.selectedTags))
const primarySelectSize = computed(() => tagLimits.value.maxPrimary > 1 ? Math.min(Math.max(primaryTags.value.length + 1, 3), 8) : undefined)
const secondarySelectSize = computed(() => tagLimits.value.maxSecondary > 1 ? Math.min(Math.max(secondaryTagOptions.value.length + 1, 3), 8) : undefined)
const primaryTagPlaceholderText = computed(() => getUiCopy({
  surface: 'discussion-composer-primary-tag-placeholder',
  loadingTags: loadingTags.value,
  hasStartableTags: primaryTags.value.length > 0,
})?.text || (loadingTags.value ? '加载标签中...' : (primaryTags.value.length ? '选择主标签' : '暂无可发帖标签')))
const secondaryTagPlaceholderText = computed(() => getUiCopy({
  surface: 'discussion-composer-secondary-tag-placeholder',
  hasSecondaryOptions: secondaryTagOptions.value.length > 0,
})?.text || (secondaryTagOptions.value.length ? '选择次标签（可选）' : '无可用次标签'))

watch(
  () => props.requestId,
  () => prepareField(),
  { immediate: true }
)

async function prepareField() {
  await ensureTagsLoaded()

  primaryTagIds.value = normalizeSelectionIds(props.state?.primaryTagIds || [props.state?.primaryTagId])
  secondaryTagIds.value = normalizeSelectionIds(props.state?.secondaryTagIds || [props.state?.secondaryTagId])
  if (!primaryTagIds.value.length && !secondaryTagIds.value.length && props.state?.requestedTagId) {
    applyRequestedTag(props.state.requestedTagId)
    return
  }

  handlePrimaryTagChange()
}

async function ensureTagsLoaded() {
  if (loadingTags.value || tags.value.length) return

  loadingTags.value = true
  syncState()
  try {
    const response = await api.get('/tags', {
      params: {
        include_children: true,
        purpose: 'start_discussion',
      },
    })
    tags.value = unwrapTagList(response).map(normalizeTag)
  } catch (error) {
    console.error('加载标签失败:', error)
  } finally {
    loadingTags.value = false
    syncState()
  }
}

function applyRequestedTag(tagId) {
  const requested = resolveRequestedSelection(tagId, tags.value)
  primaryTagIds.value = requested.primaryTagIds.map(String)
  secondaryTagIds.value = requested.secondaryTagIds.map(String)
  handlePrimaryTagChange()
}

function readSelectedOptionIds(event) {
  const select = event?.target
  if (!select?.multiple) {
    return select?.value ? [String(select.value)] : []
  }
  return Array.from(select.selectedOptions || [])
    .map(option => String(option.value || ''))
    .filter(Boolean)
}

function handlePrimaryTagChange(event) {
  if (event) {
    primaryTagIds.value = readSelectedOptionIds(event)
  }
  const allowedSecondaryIds = new Set(secondaryTagOptions.value.map(tag => Number(tag.id)))
  secondaryTagIds.value = normalizeSelectionIds(secondaryTagIds.value)
    .filter(tagId => allowedSecondaryIds.has(tagId))
    .map(String)
  syncState()
}

function handleSecondaryTagChange(event) {
  secondaryTagIds.value = readSelectedOptionIds(event)
  syncState()
}

function syncState() {
  const normalizedPrimaryIds = selectionState.value.selectedPrimaryIds.map(String)
  const normalizedSecondaryIds = selectionState.value.selectedSecondaryIds.map(String)
  if (normalizedPrimaryIds.join(',') !== normalizeSelectionIds(primaryTagIds.value).join(',')) {
    primaryTagIds.value = normalizedPrimaryIds
  }
  if (normalizedSecondaryIds.join(',') !== normalizeSelectionIds(secondaryTagIds.value).join(',')) {
    secondaryTagIds.value = normalizedSecondaryIds
  }
  props.updateState({
    availablePrimaryTagCount: primaryTags.value.length,
    availableTagCount: primaryTags.value.length + rootSecondaryTags.value.length,
    loadingTags: loadingTags.value,
    maxPrimaryTagCount: tagLimits.value.maxPrimary,
    maxSecondaryTagCount: tagLimits.value.maxSecondary,
    minPrimaryTagCount: tagLimits.value.minPrimary,
    minSecondaryTagCount: tagLimits.value.minSecondary,
    primaryTagId: normalizedPrimaryIds[0] || '',
    primaryTagIds: normalizedPrimaryIds,
    secondaryTagId: normalizedSecondaryIds[0] || '',
    secondaryTagIds: normalizedSecondaryIds,
    selectedTagCount: selectedTagIds.value.length,
    selectedTagIds: selectedTagIds.value,
    selectedTagLabel: selectedTagLabel.value,
  })
}
</script>

<style scoped>
.composer-tag-fields {
  display: contents;
}
</style>
