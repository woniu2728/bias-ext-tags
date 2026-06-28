import test from 'node:test'
import assert from 'node:assert/strict'
import {
  isChildTag,
  isPrimaryRootTag,
  isSecondaryRootTag,
  normalizeTag,
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

test('tag structure sorting keeps positioned tags before nullable tags', () => {
  const tags = [
    { name: '次级', position: null },
    { name: '主二', position: 2 },
    { name: '主一', position: 1 },
  ].sort(sortTagsByStructure)

  assert.deepEqual(tags.map(tag => tag.name), ['主一', '主二', '次级'])
})
