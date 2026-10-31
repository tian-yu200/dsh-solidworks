import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mergeManagedPatch, managedPatchMatches, repairKnownGptVision, syncEmbeddedPersona, embeddedPersonaMatches } from '../SolidWorksProfilePatch.mjs';

const mcp = { id: 'mcp-solidworks-orchestrator', name: '@deepseek-ai/dsh-mcp-client', config: { command: 'new/python', args: ['new/server.py'], cwd: 'new/adapter', serverName: 'solidworks', transport: 'stdio', toolCallTimeoutMs: 300000, failOnStartupError: true } };
const template = [{ insert: [mcp] }, { id: 'agent-presets', config: { default: 'solidworks-cad' } }];

test('tauri prefix and text both follow the source persona, preserving other presets and secrets', () => {
  const preset = [{ id: 'persona', config: { text: 'new persona\n规则\n' } }];
  const rows = [{ insert: [{ id: 'preset-solidworks-cad', config: { plugins: [
    { id: 'persona', config: { prefix: 'old prefix', text: 'old text', other: 7 } },
    { id: 'another-tool', config: { secret: 'fixture-secret' } },
  ] } }, { id: 'another-preset', config: { text: 'keep' } }] },
  { id: 'llm-pi-ai', config: { providers: { gpt: { apiKey: 'fixture-secret' } } } }];
  const before = structuredClone(rows);
  const synced = syncEmbeddedPersona(rows, preset);
  assert.equal(embeddedPersonaMatches(rows, preset), false);
  assert.equal(embeddedPersonaMatches(synced, preset), true);
  assert.deepEqual(rows, before);
  assert.deepEqual(synced[0].insert[0].config.plugins[0].config,
    { prefix: preset[0].config.text, text: preset[0].config.text, other: 7 });
  assert.deepEqual(synced[0].insert[0].config.plugins[1], rows[0].insert[0].config.plugins[1]);
  assert.deepEqual(synced[0].insert[1], rows[0].insert[1]);
  assert.deepEqual(synced[1], rows[1]);
  assert.deepEqual(syncEmbeddedPersona(synced, preset), synced);
  assert.throws(() => syncEmbeddedPersona([], preset), /exactly one/);
  assert.throws(() => syncEmbeddedPersona(rows, []), /one nonempty/);
});

test('installation preserves custom providers, default model, and unrelated MCPs', () => {
  const custom = [
    { insert: [{ id: 'another-mcp', config: { headers: { Authorization: 'fixture-secret' } } }, { ...mcp, config: { ...mcp.config, command: 'old/python', customOption: 7 } }] },
    { id: 'agent-presets', config: { default: 'old', customPresetOption: true } },
    { id: 'llm-pi-ai', config: { providers: { custom: { models: [{ id: 'vision', input: ['text', 'image'] }] } } } },
    { id: 'agent-default-model', config: { provider: 'custom', model: 'vision' } },
  ];
  const before = structuredClone(custom);
  const merged = mergeManagedPatch(custom, template);
  assert.deepEqual(custom, before, 'source data must stay untouched');
  assert.deepEqual(merged.slice(2), before.slice(2));
  assert.deepEqual(merged[0].insert[0], before[0].insert[0]);
  assert.equal(merged[0].insert[1].config.customOption, 7);
  assert.equal(merged[1].config.customPresetOption, true);
  assert.equal(managedPatchMatches(before, template), false);
  assert.equal(managedPatchMatches(merged, template), true);
  assert.deepEqual(mergeManagedPatch(merged, template), merged);
});

test('an existing preset row does not hide a missing MCP insertion', () => {
  const merged = mergeManagedPatch([{ id: 'agent-presets', config: { default: 'solidworks-cad' } }], template);
  assert.equal(managedPatchMatches(merged, template), true);
  assert.equal(merged.filter(row => row.insert).length, 1);
});

test('checks managed fields semantically and ignores unrelated plugin configuration', () => {
  const extra = [...structuredClone(template), { id: 'llm-pi-ai', config: { providers: {} } }];
  assert.equal(managedPatchMatches(extra, template), true);
  extra.push({ id: mcp.id, config: { ...mcp.config, cwd: 'stale/path' } });
  assert.equal(managedPatchMatches(extra, template), false);
  const fixed = mergeManagedPatch(extra, template);
  assert.equal(managedPatchMatches(fixed, template), true);
  assert.deepEqual(fixed.at(-2), extra.at(-2));
});

test('conflicting duplicate managed insertions and invalid YAML shapes are refused', () => {
  assert.throws(() => mergeManagedPatch([{ insert: [mcp, mcp] }], template), /duplicate/);
  assert.throws(() => mergeManagedPatch({ config: {} }, template), /sequence/);
});

test('known GPT models get explicit vision input and installation preserves every unrelated field', () => {
  const rows = [{ id: 'llm-pi-ai', config: { providers: {
    gpt: { apiKey: 'fixture-secret', models: [{ id: 'gpt-5.6-sol' }, { id: 'gpt-6-sol', input: ['text'] }, { id: 'gpt-6.1-sol', contextWindow: 100 }, { id: 'unknown-gpt' }], modelOverrides: { 'gpt-6.1-sol': { maxTokens: 5 } } },
    custom: { models: [{ id: 'text-only', input: ['text'] }] },
  } } }, { id: 'agent-default-model', config: { provider: 'gpt', model: 'gpt-6.1-sol' } }];
  const before = structuredClone(rows);
  const fixed = repairKnownGptVision(rows);
  assert.deepEqual(rows, before);
  for (const model of fixed[0].config.providers.gpt.models.slice(0, 3)) assert.deepEqual(model.input, ['text', 'image']);
  assert.deepEqual(fixed[0].config.providers.gpt.models[3], before[0].config.providers.gpt.models[3]);
  assert.deepEqual(fixed[0].config.providers.custom, before[0].config.providers.custom);
  assert.equal(fixed[0].config.providers.gpt.apiKey, 'fixture-secret');
  assert.deepEqual(fixed[1], before[1]);
  assert.deepEqual(repairKnownGptVision(fixed), fixed);
  const installed = mergeManagedPatch(fixed, template);
  assert.deepEqual(installed[0], fixed[0]);
  assert.equal(installed[0].config.providers.gpt.modelOverrides['gpt-6.1-sol'].maxTokens, 5);
  assert.deepEqual(installed[0].config.providers.gpt.modelOverrides['gpt-6.1-sol'].input, ['text', 'image']);
});
