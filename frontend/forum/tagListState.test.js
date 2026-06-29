import test from 'node:test'
import assert from 'node:assert/strict'
import {
  getCachedTagTree,
  loadTagTree,
  resetTagTreeState,
} from './tagListState.js'

test('tag list state deduplicates concurrent tag tree loads', async () => {
  resetTagTreeState()
  let fetchCount = 0
  const fetchTags = async () => {
    fetchCount += 1
    return [{ id: 1, name: '公告', children: [] }]
  }

  const [first, second] = await Promise.all([
    loadTagTree({ fetchTags }),
    loadTagTree({ fetchTags }),
  ])

  assert.equal(fetchCount, 1)
  assert.equal(first, second)
  assert.deepEqual(getCachedTagTree().map(tag => tag.id), [1])
})

test('tag list state reuses cached tag tree for follow-up loads', async () => {
  resetTagTreeState()
  let fetchCount = 0
  const fetchTags = async () => {
    fetchCount += 1
    return [{ id: fetchCount, name: `标签 ${fetchCount}`, children: [] }]
  }

  const first = await loadTagTree({ fetchTags })
  const second = await loadTagTree({ fetchTags })

  assert.equal(fetchCount, 1)
  assert.equal(second, first)
  assert.deepEqual(second.map(tag => tag.id), [1])
})

test('tag list state force reloads cached tag tree', async () => {
  resetTagTreeState()
  let fetchCount = 0
  const fetchTags = async () => {
    fetchCount += 1
    return [{ id: fetchCount, name: `标签 ${fetchCount}`, children: [] }]
  }

  await loadTagTree({ fetchTags })
  const reloaded = await loadTagTree({ force: true, fetchTags })

  assert.equal(fetchCount, 2)
  assert.deepEqual(reloaded.map(tag => tag.id), [2])
})
