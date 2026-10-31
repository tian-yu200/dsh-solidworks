import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const script = fileURLToPath(new URL('../Check-ModelImageCapability.mjs', import.meta.url));
function run(t, config, settings, extraArgs = []) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sw-image-check-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const write = (relative, content) => {
    const dest = path.join(dir, relative);
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.writeFileSync(dest, content);
  };
  write('node_modules/js-yaml/dist/js-yaml.mjs', `
    class Type {}
    const DEFAULT_SCHEMA = { extend() { return this; } };
    function load(source) {
      if (source.startsWith('MALFORMED')) {
        const error = new Error('bad YAML near apiKey: fixture-secret-do-not-print');
        error.mark = { line: 2, column: 4 };
        throw error;
      }
      return JSON.parse(source);
    }
    export default { Type, DEFAULT_SCHEMA, load };
  `);
  const model = 'node_modules/@earendil-works/pi-ai';
  write(model + '/package.json', JSON.stringify({ type: 'module' }));
  write(model + '/dist/providers/all.js', `
    export const getBuiltinProviders = () => ['catalog'];
    export const getBuiltinModels = () => [{ id: 'text', input: ['text'] }, { id: 'vision', input: ['text','image'] }];
  `);
  const adapter = 'node_modules/@deepseek-ai/dsh-llm-pi-ai';
  write(adapter + '/package.json', JSON.stringify({ type: 'module' }));
  write(adapter + '/lib/index.js', `
    export const Config = (raw = {}) => ({ providers: Object.fromEntries(Object.entries(raw.providers ?? {}).map(([name,p]) => [name,{ defaultInput:['text'],...p }])) });
  `);
  write('dump.yml', typeof config === 'string' ? config : JSON.stringify(config));
  if (settings) write('settings.yml', JSON.stringify(settings));
  const result = spawnSync(process.execPath, [script, '--profile-dump', path.join(dir,'dump.yml'), '--profiles', dir, '--settings', path.join(dir,'settings.yml'), ...extraArgs], { encoding: 'utf8' });
  return { status: result.status, output: result.stdout + result.stderr };
}

function dump(providers, provider, model) {
  return [{ id: 'llm-pi-ai', config: { providers } }, { id: 'agent-default-model', config: { provider, model } }, { id: 'settings', name: '@deepseek-ai/dsh-settings-file' }];
}

test('catalog-only default models must actually accept images', t => {
  const result = run(t, dump({ catalog: {} }, 'catalog', 'text'));
  assert.equal(result.status, 1, result.output);
  assert.match(result.output, /without "image"/);
});

test('catalog vision default and fully declared custom providers pass', t => {
  assert.equal(run(t, dump({ catalog: {} }, 'catalog', 'vision')).status, 0);
  assert.equal(run(t, dump({ custom: { models: [{ id:'vision',input:['text','image'] }] } }, 'custom', 'vision')).status, 0);
});

test('runtime settings override the composed profile default', t => {
  const result = run(t, dump({ catalog: {} }, 'catalog', 'vision'), { 'agent-default-model': { provider:'catalog',model:'text' } });
  assert.equal(result.status, 1, result.output);
  assert.match(result.output, /default model catalog[/]text/);
});

test('settings provider definitions merge over the profile instead of replacing it', t => {
  const config = dump({ catalog:{} }, 'custom', 'vision');
  const settings = { 'llm-pi-ai': { providers: { custom: { models:[{ id:'vision',input:['text','image'] }] } } } };
  const result = run(t, config, settings);
  assert.equal(result.status, 0, result.output);
  assert.match(result.output, /catalog: catalog route/);
});

test('YAML diagnostics never echo the source or parser message with credentials', t => {
  const result = run(t, 'MALFORMED apiKey: fixture-secret-do-not-print');
  assert.equal(result.status, 2);
  assert.doesNotMatch(result.output, /fixture-secret-do-not-print/);
  assert.match(result.output, /ERROR:.*line 3/);
});

test('missing plugin configuration is reported as a visible setup error', t => {
  const result = run(t, []);
  assert.equal(result.status, 2);
  assert.match(result.output, /ERROR:/);
});


test('provider scope verifies GPT without changing unrelated text-only providers', t => {
  const config = dump({ gpt: { models: [{ id: 'gpt-6.1-sol', input: ['text','image'] }] }, unrelated: { models: [{ id: 'unclassified' }] } }, 'gpt', 'gpt-6.1-sol');
  assert.equal(run(t, config).status, 1, 'unscoped audit must still flag an undeclared provider');
  const scoped = run(t, config, undefined, ['--provider', 'gpt']);
  assert.equal(scoped.status, 0, scoped.output);
  assert.match(scoped.output, /image capability: OK.*provider gpt/);
  assert.doesNotMatch(scoped.output, /unclassified/);
  assert.equal(run(t, config, undefined, ['--provider', 'missing']).status, 2);
});
