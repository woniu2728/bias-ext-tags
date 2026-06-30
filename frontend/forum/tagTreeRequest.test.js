import test from 'node:test'
import assert from 'node:assert/strict'
import { buildTagTreeRequestOptions } from './tagTreeRequest.js'

test('tag tree request asks for flarum tag page relationships', () => {
  assert.deepEqual(buildTagTreeRequestOptions(), {
    params: {
      include: 'children,lastPostedDiscussion,parent',
      include_children: true,
    },
  })
})
