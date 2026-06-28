import test from 'node:test'
import assert from 'node:assert/strict'
import { createTagSearchSource } from './tagSearchSource.js'

test('tag search source exposes backend tag search contract', () => {
  const source = createTagSearchSource()

  assert.equal(source.type, 'tag')
  assert.equal(source.routeType, 'tag')
  assert.equal(source.apiType, 'tag')
  assert.equal(source.resourceType, 'tags')
  assert.equal(source.resultsKey, 'tags')
  assert.equal(source.totalKey, 'tag_total')
  assert.equal(source.filterTarget, 'tag')
})

test('tag search source builds navigable tag result items', () => {
  const source = createTagSearchSource()
  const [item] = source.buildResultItems([
    {
      id: 7,
      name: 'Support',
      slug: 'support',
      description: 'Support requests',
      discussion_count: 12,
    },
  ], { query: 'sup' })

  assert.equal(item.key, 'tag-7')
  assert.equal(item.path, '/t/support')
  assert.equal(item.titleText, 'Support')
  assert.deepEqual(item.metaItems, ['/support', '12 条讨论'])
  assert.match(item.titleHtml, /Support|mark/)
})
