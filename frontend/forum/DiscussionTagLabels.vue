<template>
  <div class="discussion-tag-labels">
    <TagBadge
      v-for="tag in sortedTags"
      :key="tag.id"
      :tag="tag"
      :size="size"
      :max-width="maxWidth"
    />
  </div>
</template>

<script setup>
import { computed } from '@bias/core'
import TagBadge from './TagBadge.vue'
import { sortTags } from './tagUtils.js'

const props = defineProps({
  tags: { type: Array, default: () => [] },
  size: { type: String, default: 'sm' },
  maxWidth: { type: String, default: '160px' },
})

const sortedTags = computed(() => sortTags(props.tags))
</script>

<style scoped>
.discussion-tag-labels {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  min-width: 0;
  max-width: 100%;
}
</style>
