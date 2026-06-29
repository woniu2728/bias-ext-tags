<template>
  <div class="Modal Modal--small Modal--simple fade" :class="{ in: showing }" @click.stop>
    <div class="Modal-content">
      <div class="Modal-close">
        <button
          type="button"
          class="Button Button--icon Button--link"
          :aria-label="closeLabelText"
          @click="modalStore.dismiss()"
        >
          <i class="fas fa-times"></i>
        </button>
      </div>

      <div class="Modal-header">
        <h3>{{ titleText }}</h3>
      </div>

      <div class="Modal-body">
        <p class="modal-form-description">{{ descriptionText }}</p>

        <div v-if="errorMessage" class="modal-form-error">
          {{ errorMessage }}
        </div>

        <div class="modal-form-group">
          <label for="tag-discussion-primary">{{ primaryLabelText }}</label>
          <select
            id="tag-discussion-primary"
            name="primary_tag_id"
            class="modal-form-control"
            :multiple="tagLimits.maxPrimary > 1"
            :size="primarySelectSize"
            :disabled="loading || submitting || tagLimits.maxPrimary <= 0 || !primaryTags.length"
            @change="handlePrimaryTagChange"
          >
            <option value="">{{ primaryPlaceholderText }}</option>
            <option
              v-for="tag in primaryTags"
              :key="tag.id"
              :selected="form.primary_tag_ids.includes(String(tag.id))"
              :value="String(tag.id)"
            >
              {{ tag.name }}
            </option>
          </select>
        </div>

        <div class="modal-form-group">
          <label for="tag-discussion-secondary">{{ secondaryLabelText }}</label>
          <select
            id="tag-discussion-secondary"
            name="secondary_tag_id"
            class="modal-form-control"
            :multiple="tagLimits.maxSecondary > 1"
            :size="secondarySelectSize"
            :disabled="loading || submitting || tagLimits.maxSecondary <= 0 || !secondaryTagOptions.length"
            @change="handleSecondaryTagChange"
          >
            <option value="">{{ secondaryPlaceholderText }}</option>
            <option
              v-for="tag in secondaryTagOptions"
              :key="tag.id"
              :selected="form.secondary_tag_ids.includes(String(tag.id))"
              :value="String(tag.id)"
            >
              {{ tag.name }}
            </option>
          </select>
          <p class="modal-form-help">{{ secondaryHelpText }}</p>
        </div>
      </div>

      <div class="Modal-footer Modal-footer--split">
        <button type="button" class="Button Button--secondary" :disabled="submitting" @click="modalStore.dismiss()">
          {{ cancelButtonText }}
        </button>
        <button type="button" class="Button Button--primary" :disabled="!canSubmit" @click="submit">
          {{ submitButtonText }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import {
  api,
  ref,
  computed,
  onMounted,
  reactive,
  useModalStore
} from '@bias/core'
import {
  getUiCopy,
  useForumStore,
} from '@bias/core/forum'
import { normalizeDiscussion } from '@bias/discussions'
import {
  flattenTags,
  isPrimaryRootTag,
  isSecondaryRootTag,
  normalizeTag,
  unwrapTagList,
} from './tagUtils.js'
import {
  createTagSelectionState,
  normalizeSelectionIds,
  resolveTagSelectionLimits,
} from './tagSelectionState.js'

const props = defineProps({
  discussion: {
    type: Object,
    default: null
  },
  showing: {
    type: Boolean,
    default: false
  }
})

const modalStore = useModalStore()
const loading = ref(false)
const submitting = ref(false)
const errorMessage = ref('')
const tags = ref([])
const forumStore = useForumStore()
const form = reactive({
  primary_tag_ids: [],
  secondary_tag_ids: [],
})

const tagLimits = computed(() => resolveTagSelectionLimits(forumStore.settings || {}))
const selectionState = computed(() => createTagSelectionState({
  tags: tags.value,
  primaryTagIds: form.primary_tag_ids,
  secondaryTagIds: form.secondary_tag_ids,
  settings: forumStore.settings || {},
}))
const primaryTags = computed(() => selectionState.value.primaryTags)
const secondaryTagOptions = computed(() => selectionState.value.secondaryOptions)
const selectedTagIds = computed(() => selectionState.value.selectedTagIds)
const tagSelectionRequirement = computed(() => selectionState.value.requirement)
const primarySelectSize = computed(() => tagLimits.value.maxPrimary > 1 ? Math.min(Math.max(primaryTags.value.length + 1, 3), 8) : undefined)
const secondarySelectSize = computed(() => tagLimits.value.maxSecondary > 1 ? Math.min(Math.max(secondaryTagOptions.value.length + 1, 3), 8) : undefined)
const hasChanged = computed(() => {
  const original = unwrapTagList(props.discussion?.tags)
    .map(tag => Number(tag?.id))
    .filter(Number.isInteger)
    .sort((left, right) => left - right)
  const selected = [...selectedTagIds.value].sort((left, right) => left - right)
  return original.length !== selected.length || original.some((value, index) => value !== selected[index])
})
const canSubmit = computed(() => {
  return Boolean(
    props.discussion?.id
    && !tagSelectionRequirement.value
    && !loading.value
    && !submitting.value
    && hasChanged.value
  )
})
const closeLabelText = computed(() => getUiCopy({ surface: 'tag-discussion-close-label' })?.text || '关闭')
const titleText = computed(() => getUiCopy({ surface: 'tag-discussion-title' })?.text || '编辑讨论标签')
const descriptionText = computed(() => getUiCopy({
  surface: 'tag-discussion-description',
  discussionTitle: props.discussion?.title || '',
})?.text || '调整这个讨论归属的主标签和次标签。')
const primaryLabelText = computed(() => getUiCopy({ surface: 'tag-discussion-primary-label' })?.text || '主标签')
const secondaryLabelText = computed(() => getUiCopy({ surface: 'tag-discussion-secondary-label' })?.text || '次标签')
const primaryPlaceholderText = computed(() => getUiCopy({
  surface: 'tag-discussion-primary-placeholder',
  loading: loading.value,
  hasTags: primaryTags.value.length > 0,
})?.text || (loading.value ? '加载标签中...' : (primaryTags.value.length ? '选择主标签' : '暂无可用主标签')))
const secondaryPlaceholderText = computed(() => getUiCopy({
  surface: 'tag-discussion-secondary-placeholder',
  hasSecondaryOptions: secondaryTagOptions.value.length > 0,
})?.text || (secondaryTagOptions.value.length ? '不选择次标签' : '无可用次标签'))
const secondaryHelpText = computed(() => getUiCopy({
  surface: 'tag-discussion-secondary-help',
})?.text || tagSelectionRequirement.value?.message || '次标签必须隶属于当前主标签。')
const cancelButtonText = computed(() => getUiCopy({ surface: 'modal-cancel-button' })?.text || '取消')
const submitButtonText = computed(() => getUiCopy({
  surface: 'tag-discussion-submit-button',
  submitting: submitting.value,
})?.text || (submitting.value ? '保存中...' : '保存标签'))

onMounted(async () => {
  syncDiscussionTags()
  await loadTags()
})

async function loadTags() {
  loading.value = true
  errorMessage.value = ''

  try {
    const response = await api.get('/tags', {
      params: {
        discussion_id: props.discussion?.id,
        include_children: true,
        purpose: 'add_to_discussion',
      },
    })
    tags.value = unwrapTagList(response).map(normalizeTag)
    syncDiscussionTags()
  } catch (error) {
    errorMessage.value = error.response?.data?.error || error.message || (getUiCopy({
      surface: 'tag-discussion-load-error',
    })?.text || '标签加载失败，请稍后重试')
  } finally {
    loading.value = false
  }
}

function syncDiscussionTags() {
  const currentTags = flattenTags(props.discussion?.tags || [])
  form.primary_tag_ids = currentTags.filter(isPrimaryRootTag).map(tag => String(tag.id))
  form.secondary_tag_ids = currentTags.filter(isSecondaryRootTag).map(tag => String(tag.id))
  for (const tag of currentTags) {
    if (tag?.parent_id) {
      form.secondary_tag_ids.push(String(tag.id))
    }
  }
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
    form.primary_tag_ids = readSelectedOptionIds(event)
  }
  const allowedSecondaryIds = new Set(secondaryTagOptions.value.map(tag => Number(tag.id)))
  form.primary_tag_ids = selectionState.value.selectedPrimaryIds.map(String)
  form.secondary_tag_ids = normalizeSelectionIds(form.secondary_tag_ids)
    .filter(tagId => allowedSecondaryIds.has(tagId))
    .slice(0, tagLimits.value.maxSecondary)
    .map(String)
}

function handleSecondaryTagChange(event) {
  form.secondary_tag_ids = readSelectedOptionIds(event)
  form.secondary_tag_ids = selectionState.value.selectedSecondaryIds.map(String)
}

async function submit() {
  if (!canSubmit.value) return

  submitting.value = true
  errorMessage.value = ''

  try {
    const result = await api.patch(
      `/discussions/${props.discussion.id}`,
      {
        data: {
          type: 'discussion',
          relationships: {
            tags: {
              data: selectedTagIds.value.map(tagId => ({
                type: 'tag',
                id: String(tagId),
              })),
            },
          },
        },
      },
    )
    modalStore.close({
      updated: true,
      discussion: normalizeDiscussion(result),
    })
  } catch (error) {
    errorMessage.value = error.response?.data?.error
      || error.response?.data?.detail
      || error.response?.data?.relationships?.tags?.[0]
      || error.message
      || (getUiCopy({ surface: 'modal-submit-error' })?.text || '提交失败，请稍后重试')
  } finally {
    submitting.value = false
  }
}
</script>
