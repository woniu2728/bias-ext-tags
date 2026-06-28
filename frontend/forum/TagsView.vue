<template>
  <div class="tags-page">
    <ForumPageWithSidebar>
      <template #sidebar>
        <DiscussionListSidebarStartButton
          v-if="sidebarBindings.showStartDiscussionButton"
          :current-tag="null"
          :start-discussion-button-style="{}"
          @click="sidebarEvents.startDiscussion"
        />

        <ForumPrimaryNav :auth-store="sidebarBindings.authStore" :notification-store="null" active-key="tags" />
      </template>

      <main class="tags-content">
        <ForumHeroPanel v-bind="heroBindings" />

        <ForumStateBlock v-if="contentBindings.loading">{{ contentBindings.loadingStateText }}</ForumStateBlock>
        <ForumStateBlock v-else-if="contentBindings.tags.length === 0">{{ contentBindings.emptyStateText }}</ForumStateBlock>

        <template v-else>
          <section v-if="contentBindings.primaryTags.length" class="tags-section">
            <div class="tag-grid">
              <TagTile
                v-for="tag in contentBindings.primaryTags"
                :key="tag.id"
                :tag="tag"
              />
            </div>
          </section>

          <section v-if="contentBindings.secondaryTags.length" class="tags-section tags-section--secondary">
            <h2 class="tags-section-title">更多标签</h2>
            <TagCloud :tags="contentBindings.secondaryTags" />
          </section>
        </template>
      </main>
    </ForumPageWithSidebar>
  </div>
</template>

<script setup>
import {
  useAuthStore } from '@bias/users'
import { useRouter } from '@bias/core'
import {
  ForumHeroPanel,
  ForumPageWithSidebar,
  ForumPrimaryNav,
  ForumStateBlock,
  useComposerStore,
  useForumStore
} from '@bias/core/forum'
import { DiscussionListSidebarStartButton } from '@bias/discussions'
import TagCloud from './TagCloud.vue'
import TagTile from './TagTile.vue'
import { useTagsViewModel } from './useTagsViewModel.js'

const authStore = useAuthStore()
const composerStore = useComposerStore()
const forumStore = useForumStore()
const router = useRouter()
const {
  contentBindings,
  heroBindings,
  sidebarBindings,
  sidebarEvents,
} = useTagsViewModel({
  authStore,
  composerStore,
  forumStore,
  router,
})
</script>

<style scoped>
.tags-page {
  background: var(--forum-bg-canvas);
  min-height: calc(100vh - 56px);
}

.tags-content {
  padding: 24px 28px 40px;
}

.tag-grid {
  display: grid;
  gap: 18px;
}

.tags-section + .tags-section {
  margin-top: 28px;
}

.tags-section-title {
  margin: 0 0 14px;
  color: var(--forum-text-color);
  font-size: 18px;
  font-weight: 600;
}
</style>
