import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { isDeepStrictEqual } from 'node:util';

function sequence(rows) {
  if (!Array.isArray(rows)) throw new Error('profile patch must be a YAML sequence');
  if (rows.some(row => !row || typeof row !== 'object' || Array.isArray(row))) {
    throw new Error('profile patch rows must be mappings');
  }
  return rows;
}

function inserted(entries, visit) {
  for (const entry of sequence(entries)) {
    visit(entry);
    if (entry.group && Array.isArray(entry.config)) inserted(entry.config, visit);
  }
}

function occurrences(rows, id) {
  const found = [];
  for (const row of rows) {
    if (row.insert) {
      inserted(row.insert, entry => { if (entry.id === id) found.push({ row: entry, insert: true }); });
    } else if (row.id === id) found.push({ row, insert: false });
  }
  return found;
}

function managedEntries(template) {
  return sequence(template).flatMap(row => row.insert
    ? row.insert.map(entry => ({ row: entry, insert: true }))
    : [{ row, insert: false }]);
}

function clone(value) {
  if (Array.isArray(value)) return value.map(clone);
  // Preserve inert YAML expression objects and their dump tag.
  if (value && Object.getPrototypeOf(value) === Object.prototype) {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, clone(item)]));
  }
  return value;
}

function mergeFields(target, managed) {
  for (const [key, value] of Object.entries(managed)) {
    if (value && Object.getPrototypeOf(value) === Object.prototype) {
      if (!target[key] || Object.getPrototypeOf(target[key]) !== Object.prototype) target[key] = {};
      mergeFields(target[key], value);
    } else target[key] = clone(value);
  }
}

function includesFields(actual, expected) {
  return Object.entries(expected).every(([key, value]) =>
    value && Object.getPrototypeOf(value) === Object.prototype
      ? actual?.[key] && includesFields(actual[key], value)
      : isDeepStrictEqual(actual?.[key], value));
}

export const KNOWN_GPT_VISION_MODELS = Object.freeze(['gpt-5.6-sol', 'gpt-6-sol', 'gpt-6.1-sol']);

export function repairKnownGptVision(rows) {
  const result = clone(sequence(rows));
  for (const { row } of occurrences(result, 'llm-pi-ai')) {
    const provider = row.config?.providers?.gpt;
    if (!provider) continue;
    for (const model of provider.models ?? []) {
      if (KNOWN_GPT_VISION_MODELS.includes(model.id)) model.input = ['text', 'image'];
    }
    for (const [id, model] of Object.entries(provider.modelOverrides ?? {})) {
      if (KNOWN_GPT_VISION_MODELS.includes(id)) model.input = ['text', 'image'];
    }
  }
  return result;
}

function presetPersona(preset) {
  const found = sequence(preset).filter(row => row.id === 'persona');
  const text = found[0]?.config?.text;
  if (found.length !== 1 || typeof text !== 'string' || !text.trim()) {
    throw new Error('source preset must contain one nonempty persona text');
  }
  return text;
}

export function syncEmbeddedPersona(rows, preset) {
  const text = presetPersona(preset);
  const result = clone(sequence(rows));
  const found = occurrences(result, 'preset-solidworks-cad');
  if (found.filter(item => item.insert).length !== 1) {
    throw new Error('tauri must contain exactly one preset-solidworks-cad insertion');
  }
  for (const { row } of found) {
    const plugins = row.config?.plugins;
    if (!Array.isArray(plugins)) throw new Error('embedded SolidWorks preset has no plugins');
    const personas = plugins.filter(plugin => plugin.id === 'persona');
    if (personas.length !== 1) throw new Error('embedded SolidWorks preset must contain one persona');
    const config = personas[0].config ??= {};
    config.prefix = text;
    config.text = text;
  }
  return result;
}

export function embeddedPersonaMatches(rows, preset) {
  return isDeepStrictEqual(rows, syncEmbeddedPersona(rows, preset));
}

export function managedPatchMatches(rows, template) {
  sequence(rows);
  return managedEntries(template).every(managed => {
    const found = occurrences(rows, managed.row.id);
    if (managed.insert && found.filter(item => item.insert).length !== 1) return false;
    if (!found.length) return false;
    // DSH replaces each field (including config) shallowly, in patch order.
    const effective = {};
    for (const item of found) {
      if (!item.insert && item.row.name && effective.name && item.row.name !== effective.name) continue;
      Object.assign(effective, item.row);
    }
    return includesFields(effective, managed.row);
  });
}

export function mergeManagedPatch(rows, template) {
  const result = clone(sequence(rows));
  for (const managed of managedEntries(template)) {
    let found = occurrences(result, managed.row.id);
    if (found.filter(item => item.insert).length > 1) throw new Error('duplicate managed plugin insertion');
    if (managed.insert && !found.some(item => item.insert)) {
      result.push({ insert: [clone(managed.row)] });
      continue;
    }
    if (!found.length) {
      result.push(clone(managed.row));
      continue;
    }
    const definition = found.find(item => item.insert);
    if (definition) mergeFields(definition.row, managed.row);
    const last = found.at(-1);
    if (last.row.name && managed.row.name && last.row.name !== managed.row.name) {
      throw new Error('managed plugin name conflicts with the integration');
    }
    mergeFields(last.row, managed.row);
  }
  if (!managedPatchMatches(result, template)) throw new Error('managed profile merge verification failed');
  return repairKnownGptVision(result);
}

export function loadDshYaml(dshRoot) {
  const manifest = fs.realpathSync(path.join(dshRoot, 'node_modules', '@deepseek-ai', 'dsh', 'package.json'));
  const yaml = createRequire(manifest)('js-yaml');
  class Expression { constructor(source) { this.source = source; } }
  const schema = yaml.DEFAULT_SCHEMA.extend([new yaml.Type('tag:yaml.org,2002:js', {
    kind: 'scalar', instanceOf: Expression,
    construct: source => new Expression(source), represent: expression => expression.source,
  })]);
  return {
    load(source) {
      try { return yaml.load(source, { schema }); }
      catch (error) {
        // YAML errors can contain the entire document, including credentials.
        const location = error.mark ? ` at line ${error.mark.line + 1}, column ${error.mark.column + 1}` : '';
        throw new Error(`invalid profile YAML${location}`);
      }
    },
    dump: data => yaml.dump(data, { schema, noRefs: true, lineWidth: 120 }),
  };
}

function main() {
  const args = process.argv.slice(2);
  const arg = name => args[args.indexOf(name) + 1];
  const required = name => { if (!args.includes(name) || !arg(name)) throw new Error(`missing ${name}`); return arg(name); };
  const yaml = loadDshYaml(required('--dsh-root'));
  if (['sync-tauri', 'check-tauri'].includes(required('--mode'))) {
    const source = required('--source');
    const original = fs.readFileSync(source, 'utf8');
    const rows = yaml.load(original);
    const preset = yaml.load(fs.readFileSync(required('--preset'), 'utf8'));
    const persona = syncEmbeddedPersona(rows, preset);
    if (required('--mode') === 'check-tauri') {
      const ok = isDeepStrictEqual(rows, persona);
      console.log(ok ? 'tauri embedded SolidWorks persona prefix/text: OK' : 'tauri embedded SolidWorks persona prefix/text: OUT OF DATE');
      process.exitCode = ok ? 0 : 1;
    } else {
      const output = repairKnownGptVision(persona);
      fs.writeFileSync(required('--destination'), isDeepStrictEqual(rows, output) ? original : yaml.dump(output), 'utf8');
      console.log('tauri persona and known GPT vision prepared; credentials not displayed');
    }
    return;
  }
  if (required('--mode') === 'repair-vision') {
    const source = required('--source');
    const original = fs.readFileSync(source, 'utf8');
    const rows = yaml.load(original);
    const repaired = repairKnownGptVision(rows);
    fs.writeFileSync(required('--destination'), isDeepStrictEqual(rows, repaired) ? original : yaml.dump(repaired), 'utf8');
    console.log('known GPT vision input declarations prepared; credentials not displayed');
    return;
  }
  let templateText = fs.readFileSync(required('--template'), 'utf8');
  for (const [token, option] of [['__PYTHON__', '--python'], ['__DSH_SERVER__', '--server'], ['__DSH_ADAPTER_DIR__', '--adapter-dir']]) {
    templateText = templateText.replaceAll(token, required(option).replaceAll('\\', '/'));
  }
  const dshHome = args.includes('--dsh-home') ? required('--dsh-home') : path.dirname(path.dirname(path.dirname(path.resolve(required('--source')))));
  templateText = templateText.replaceAll('__DSH_HOME__', dshHome.replaceAll('\\', '/'));
  const template = yaml.load(templateText);
  const source = required('--source');
  const rows = fs.existsSync(source) ? yaml.load(fs.readFileSync(source, 'utf8')) : [];
  const mode = required('--mode');
  if (mode === 'check') {
    const ok = managedPatchMatches(rows, template);
    console.log(ok ? 'managed profile configuration: OK' : 'managed profile configuration: OUT OF DATE');
    process.exitCode = ok ? 0 : 1;
  } else if (mode === 'merge') {
    const output = mergeManagedPatch(rows, template);
    // Preserve comments and exact bytes when an update is unnecessary.
    const text = isDeepStrictEqual(rows, output) ? fs.readFileSync(source, 'utf8') : yaml.dump(output);
    fs.writeFileSync(required('--destination'), text, 'utf8');
    console.log('managed profile configuration prepared; user overrides preserved');
  } else throw new Error('unknown profile patch mode');
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { main(); }
  catch (error) {
    console.error(`profile patch error: ${error.code ?? error.message}`);
    process.exitCode = 2;
  }
}
