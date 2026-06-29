import test from 'node:test'
import assert from 'node:assert/strict'
import {
  createTagSelectionState,
  resolveRequestedSelection,
  resolveTagSelectionLimits,
  summarizeSelectedTags,
} from './tagSelectionState.js'

const tags = [
  {
    id: 1,
    name: '主一',
    is_primary: true,
    parent_id: null,
    position: 0,
    children: [
      {
        id: 11,
        name: '子一',
        is_primary: true,
        parent_id: 1,
        position: 0,
        children: [
          { id: 111, name: '历史孙级', is_primary: true, parent_id: 11, position: 0 },
        ],
      },
    ],
  },
  {
    id: 2,
    name: '主二',
    is_primary: true,
    parent_id: null,
    position: 1,
    children: [
      { id: 21, name: '子二', is_primary: true, parent_id: 2, position: 0 },
    ],
  },
  { id: 3, name: '次级', is_primary: false, parent_id: null, position: null, children: [] },
]

test('tag selection limits use forum settings with defaults', () => {
  assert.deepEqual(resolveTagSelectionLimits({}), {
    minPrimary: 0,
    maxPrimary: 1,
    minSecondary: 0,
    maxSecondary: 1,
  })
  assert.deepEqual(resolveTagSelectionLimits({
    min_primary_tags: '1',
    max_primary_tags: '2',
    min_secondary_tags: 1,
    max_secondary_tags: 3,
  }), {
    minPrimary: 1,
    maxPrimary: 2,
    minSecondary: 1,
    maxSecondary: 3,
  })
})

test('tag selection supports multiple configured primary and secondary choices', () => {
  const state = createTagSelectionState({
    tags,
    primaryTagIds: [1, 2],
    secondaryTagIds: [11, 21, 3],
    settings: {
      max_primary_tags: 2,
      max_secondary_tags: 3,
    },
  })

  assert.deepEqual(state.selectedPrimaryIds, [1, 2])
  assert.deepEqual(state.selectedSecondaryIds, [11, 21, 3])
  assert.deepEqual(state.selectedTagIds, [1, 2, 11, 21, 3])
})

test('tag selection filters child tags whose parent is not selected', () => {
  const state = createTagSelectionState({
    tags,
    primaryTagIds: [1],
    secondaryTagIds: [11, 111, 21, 3],
    settings: {
      max_secondary_tags: 3,
    },
  })

  assert.deepEqual(state.secondaryOptions.map(tag => tag.id), [11, 3])
  assert.deepEqual(state.selectedSecondaryIds, [11, 3])
})

test('tag selection counts direct child tags once their primary parent is selected', () => {
  const withoutPrimary = createTagSelectionState({
    tags,
  })
  const withPrimary = createTagSelectionState({
    tags,
    primaryTagIds: [1],
  })

  assert.equal(withoutPrimary.availableTagCount, 3)
  assert.equal(withPrimary.availableTagCount, 4)
  assert.deepEqual(withPrimary.secondaryOptions.map(tag => tag.id), [11, 3])
})

test('tag selection ignores legacy grandchildren in flattened choices', () => {
  const state = createTagSelectionState({
    tags,
    primaryTagIds: [1],
    secondaryTagIds: [111],
    settings: {
      max_secondary_tags: 3,
    },
  })

  assert.equal(state.secondaryOptions.some(tag => tag.id === 111), false)
  assert.deepEqual(state.selectedSecondaryIds, [])
  assert.deepEqual(resolveRequestedSelection(111, tags), {
    primaryTagIds: [],
    secondaryTagIds: [],
  })
})

test('requested tag selection expands child to parent and child ids', () => {
  assert.deepEqual(resolveRequestedSelection(11, tags), {
    primaryTagIds: [1],
    secondaryTagIds: [11],
  })
  assert.deepEqual(resolveRequestedSelection(3, tags), {
    primaryTagIds: [],
    secondaryTagIds: [3],
  })
})

test('selected tag summary joins selected tag names', () => {
  const state = createTagSelectionState({
    tags,
    primaryTagIds: [1],
    secondaryTagIds: [11],
  })

  assert.equal(summarizeSelectedTags(state.selectedTags), '主一 / 子一')
})
