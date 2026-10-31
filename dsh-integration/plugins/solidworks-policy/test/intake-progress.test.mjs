import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createIntakeProgress, STALL_LIMIT } from '../intake-progress.js';
const name = 'mcp__solidworks__read_drawing_region';
const call = (agent, region = 'C4', extra = {}) => ({ agent, name, arguments: { image_path: 'drawing.png', region, zoom: 2, ...extra } });
const crop = (box = [300, 450, 450, 600], zoom = 2) => ({ isError: false, content: [{ type: 'text', text: JSON.stringify({ status: 'completed', action: 'cell', region: { label: 'C4', source_box: box }, zoom }) }] });
const failure = { isError: false, content: [{ type: 'text', text: '{"status":"failed","error":{"code":"ATTACHMENT_NOT_FOUND"}}' }] };

test('three consecutive redundant reads block the repeated attempt and expose the count', () => {
  const warnings = [], guard = createIntakeProgress({ warn: message => warnings.push(message) }), agent = {};
  guard.begin(agent, 1);
  guard.observe(call(agent), crop());
  for (let i = 0; i < STALL_LIMIT; i++) {
    assert.equal(guard.guard(call(agent)), undefined);
    guard.observe(call(agent), crop());
  }
  assert.match(guard.guard(call(agent)), /INTAKE_NO_PROGRESS.*3 consecutive/);
  assert.match(warnings[0], /turn=1 consecutive=3/);
  // Slightly changed but already covered boxes are not a bypass; useful zoom remains available.
  assert.match(guard.guard(call(agent, '320,470,430,580')), /INTAKE_NO_PROGRESS/);
  assert.equal(guard.guard(call(agent, 'C4', { zoom: 4 })), undefined);
});

test('a novel successful evidence read restores progress', () => {
  const guard = createIntakeProgress(), agent = {};
  guard.begin(agent, 1);
  for (let i = 0; i < STALL_LIMIT; i++) guard.observe(call(agent), failure);
  assert.match(guard.guard(call(agent)), /Report.*failure.*not label it a missing dimension/);
  const next = call(agent, 'A1');
  assert.equal(guard.guard(next), undefined);
  guard.observe(next, crop([0, 0, 150, 150]));
  assert.equal(guard.guard(call(agent)), undefined);
});

test('state is isolated by exact host Agent and host turn, not image or model arguments', () => {
  const guard = createIntakeProgress(), first = {}, second = {};
  guard.begin(first, 7); guard.begin(second, 7);
  for (let i = 0; i < STALL_LIMIT; i++) guard.observe(call(first), failure);
  assert.equal(guard.guard(call(second)), undefined);
  assert.match(guard.guard(call(first, 'C4', { reset_budget: true, task_id: 'fresh' })), /INTAKE_NO_PROGRESS/);
  guard.begin(first, 7); // same host turn must not reset
  assert.match(guard.guard(call(first)), /INTAKE_NO_PROGRESS/);
  guard.begin(first, 8);
  assert.equal(guard.guard(call(first)), undefined);
  guard.dispose(second);
});

test('new crop batches count as progress; repeated OCR excludes elapsed-time noise', () => {
  const guard = createIntakeProgress(), agent = {};
  guard.begin(agent, 1);
  const exec = { agent, name: 'mcp__solidworks__read_drawing_text', arguments: { image_path: 'drawing.png' } };
  for (let i = 0; i <= STALL_LIMIT; i++) guard.observe(exec, { isError: false, value: { status: 'completed', action: 'ocr', elapsed_s: i, texts: [{ text: '90' }] } });
  assert.match(guard.guard(exec), /INTAKE_NO_PROGRESS/);
  guard.observe(call(agent), { isError: false, value: { status: 'completed', action: 'batch', regions: [{ status: 'completed', region: { source_box: [0, 0, 200, 200] }, zoom: 3 }] } });
  assert.equal(guard.guard(exec), undefined);
});
