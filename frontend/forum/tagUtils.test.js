import test from 'node:test'
import assert from 'node:assert/strict'
import {
  flattenTags,
  groupTagsByStructure,
  isChildTag,
  isPrimaryRootTag,
  isSecondaryRootTag,
  normalizeTag,
  sortTags,
  sortTagsByStructure,
} from './tagUtils.js'

test('tag structure helpers follow stored primary state', () => {
  const primary = normalizeTag({ id: 1, is_primary: true, parent_id: null, position: 0 })
  const secondary = normalizeTag({ id: 2, is_primary: false, parent_id: null, position: null })
  const child = normalizeTag({ id: 3, is_primary: true, parent_id: 1, position: 1 })

  assert.equal(isPrimaryRootTag(primary), true)
  assert.equal(isSecondaryRootTag(secondary), true)
  assert.equal(isChildTag(child), true)
  assert.equal(isPrimaryRootTag(secondary), false)
  assert.equal(isSecondaryRootTag(child), false)
})

test('tag structure sorting follows primary, child and secondary ordering', () => {
  const parent = { id: 1, name: '主一', is_primary: true, position: 1, parent_id: null }
  const tags = [
    { id: 4, name: '低热度次级', is_primary: false, position: null, discussion_count: 1 },
    { id: 3, name: '子一', is_primary: true, position: 0, parent_id: 1, parent },
    { id: 2, name: '主二', is_primary: true, position: 2, parent_id: null },
    { id: 5, name: '高热度次级', is_primary: false, position: null, discussion_count: 9 },
    parent,
  ].sort(sortTagsByStructure)

  assert.deepEqual(tags.map(tag => tag.name), ['主一', '子一', '主二', '高热度次级', '低热度次级'])
})

test('sort tags derives parent ordering from the same tag payload', () => {
  const tags = sortTags([
    { id: 20, name: '子二', is_primary: true, parent_id: 2, position: 0 },
    { id: 2, name: '主二', is_primary: true, parent_id: null, position: 2 },
    { id: 10, name: '子一', is_primary: true, parent_id: 1, position: 0 },
    { id: 1, name: '主一', is_primary: true, parent_id: null, position: 1 },
  ])

  assert.deepEqual(tags.map(tag => tag.id), [1, 10, 2, 20])
})

test('tag structure grouping separates primary, secondary roots and children', () => {
  const grouped = groupTagsByStructure([
    { id: 1, name: '主二', is_primary: true, parent_id: null, position: 2 },
    { id: 2, name: '主一', is_primary: true, parent_id: null, position: 1 },
    { id: 3, name: '子', is_primary: true, parent_id: 2, position: 0 },
    { id: 4, name: '次级乙', is_primary: false, parent_id: null, position: null },
    { id: 5, name: '次级甲', is_primary: false, parent_id: null, position: null },
  ])

  assert.deepEqual(grouped.primaryTags.map(tag => tag.id), [2, 1])
  assert.deepEqual(grouped.primaryTags[0].children.map(tag => tag.id), [3])
  assert.deepEqual(grouped.secondaryTags.map(tag => tag.id), [4, 5])
  assert.deepEqual(grouped.childTags.map(tag => tag.id), [3])
})

test('tag structure grouping ignores legacy grandchildren', () => {
  const tags = [{
    id: 1,
    name: '主',
    is_primary: true,
    parent_id: null,
    position: 0,
    children: [{
      id: 2,
      name: '子',
      is_primary: true,
      parent_id: 1,
      position: 0,
      children: [{
        id: 3,
        name: '历史孙级',
        is_primary: true,
        parent_id: 2,
        position: 0,
      }],
    }],
  }]

  assert.deepEqual(flattenTags(tags).map(tag => tag.id), [1, 2])
  const grouped = groupTagsByStructure(tags)
  assert.deepEqual(grouped.primaryTags.map(tag => tag.id), [1])
  assert.deepEqual(grouped.primaryTags[0].children.map(tag => tag.id), [2])
  assert.deepEqual(grouped.childTags.map(tag => tag.id), [2])
})
