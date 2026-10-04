import { test, expect } from 'claude-code/testing'
import { SECTION, LESSONS } from './lessons'

test('the grounding section carries every lesson', () => {
  expect(LESSONS.length).toBe(6)
  for (const l of LESSONS) expect(SECTION.includes(l.rule)).toBe(true)
})
