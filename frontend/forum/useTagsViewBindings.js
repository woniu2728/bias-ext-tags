import { computed } from '@bias/core'


export function createTagsViewBindings({
  authStore,
  childTags = { value: [] },
  cloudTags,
  emptyStateText,
  handleStartDiscussion,
  heroDescriptionText,
  heroTitleText,
  loading,
  loadingStateText,
  showStartDiscussionButton,
  primaryTags = { value: [] },
  secondaryTags = { value: [] },
  tags,
}) {
  const sidebarBindings = computed(() => ({
    authStore,
    showStartDiscussionButton: showStartDiscussionButton.value,
  }))

  const sidebarEvents = {
    startDiscussion: handleStartDiscussion,
  }

  const heroBindings = computed(() => ({
    title: heroTitleText.value,
    description: heroDescriptionText.value,
    variant: 'default',
  }))

  const contentBindings = computed(() => ({
    childTags: childTags.value,
    cloudTags: cloudTags.value,
    emptyStateText: emptyStateText.value,
    loading: loading.value,
    loadingStateText: loadingStateText.value,
    primaryTags: primaryTags.value,
    secondaryTags: secondaryTags.value,
    tags: tags.value,
  }))

  return {
    contentBindings,
    heroBindings,
    sidebarBindings,
    sidebarEvents,
  }
}

export function useTagsViewBindings(options) {
  return createTagsViewBindings(options)
}
