// Check-ModelImageCapability.mjs

// Validates that DSH's LLM configuration keeps image (vision) input available
// for model switching. Ground truth is the installed llm-pi-ai adapter itself:
// the settings section is parsed through its own Config schema, and each
// model's effective input modalities are resolved with the same
// declared-input -> installed-catalog -> provider-defaultInput chain the
// adapter uses at request time (a message containing an image is rejected
// unless the resolved list includes "image").

// Usage:
//   node Check-ModelImageCapability.mjs [--settings <settings.yaml>] [--profiles <dsh profiles dir>]
//
// Exit codes: 0 = pass, 1 = validation failed, 2 = environment/setup error.

import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { pathToFileURL } from "node:url";

function fail(message) {
    console.error(message);
    process.exit(2);
}

function readArg(name, fallback) {
    const index = process.argv.indexOf(name);
    if (index === -1 || index + 1 >= process.argv.length) return fallback;
    return process.argv[index + 1];
}

const settingsPath = path.resolve(readArg("--settings", path.join(os.homedir(), ".dsh", "settings.yaml")));
const profilesDir = path.resolve(readArg("--profiles", path.join(os.homedir(), ".dsh", "profiles")));
const nodeModules = path.join(profilesDir, "node_modules");

function importFromNodeModules(relativePath) {
    const target = path.join(nodeModules, relativePath);
    if (!fs.existsSync(target)) fail(`required module not found under profiles dir: ${target}`);
    return import(pathToFileURL(target).href);
}

const { default: yaml } = await importFromNodeModules(path.join("js-yaml", "dist", "js-yaml.mjs"));
const piAiProviders = await importFromNodeModules(path.join("@earendil-works", "pi-ai", "dist", "providers", "all.js"));
const llmPiAi = await importFromNodeModules(path.join("@deepseek-ai", "dsh-llm-pi-ai", "lib", "index.js"));

let raw;
try {
    raw = yaml.load(fs.readFileSync(settingsPath, "utf8"));
} catch (error) {
    fail(`settings.yaml is not valid YAML: ${error.message}`);
}
if (!raw || typeof raw !== "object") fail(`settings file is empty or not a mapping: ${settingsPath}`);

let section;
try {
    section = llmPiAi.Config(raw["llm-pi-ai"]);
} catch (error) {
    fail(`llm-pi-ai settings section rejected by the installed adapter schema: ${error.message}`);
}

const providers = section.providers ?? {};
const catalogRoutes = new Set(piAiProviders.getBuiltinProviders());

// declaredInput semantics: the config schema materializes an absent `input`
// array as [], and the adapter treats absent and [] the same way — fall
// through to the catalog entry, then the provider defaultInput.
function declaredInput(entry) {
    return Array.isArray(entry.input) && entry.input.length > 0 ? entry.input : undefined;
}

function effectiveInput(provider, modelId, entry) {
    const catalog = catalogRoutes.has(provider) ? piAiProviders.getBuiltinModels(provider) : [];
    return declaredInput(entry ?? {}) ?? catalog.find((m) => m.id === modelId)?.input ?? providers[provider].defaultInput;
}

const problems = [];
const report = [];

for (const [name, provider] of Object.entries(providers)) {
    const isCatalog = catalogRoutes.has(name);
    const explicitDefault = raw["llm-pi-ai"]?.providers?.[name]?.defaultInput !== undefined;
    if (!isCatalog && !explicitDefault) {
        problems.push(
            `provider "${name}" is a custom route with no declared defaultInput; ` +
            `models without an explicit input list resolve to text-only, so image attachments will be refused ` +
            `(add "defaultInput: [ text, image ]" under the provider, or "[ text ]" to opt out deliberately)`
        );
    }
    const entries = [
        ...(provider.models ?? []).map((m) => [m.id, m]),
        ...Object.entries(provider.modelOverrides ?? {}).map(([id, o]) => [id, o]),
    ];
    if (entries.length === 0 && isCatalog) {
        report.push(`  ${name}: catalog route (${piAiProviders.getBuiltinModels(name).length} models, per-model input from the installed catalog)`);
        continue;
    }
    for (const [modelId, entry] of entries) {
        const effective = effectiveInput(name, modelId, entry);
        report.push(`  ${name}/${modelId}: effective input = ${JSON.stringify(effective)}`);
    }
}

const defaultModel = raw["agent-default-model"];
if (!defaultModel?.provider || !defaultModel?.model) {
    problems.push("agent-default-model is not set; DSH will fall back to a built-in default that may not accept images");
} else if (!providers[defaultModel.provider]) {
    problems.push(
        `agent-default-model names provider "${defaultModel.provider}", which is not configured under llm-pi-ai.providers ` +
        `(official DeepSeek adapter routes are text-only and cannot serve the CAD reference-image workflow)`
    );
} else {
    const provider = providers[defaultModel.provider];
    const entry = [
        ...(provider.models ?? []).map((m) => [m.id, m]),
        ...Object.entries(provider.modelOverrides ?? {}).map(([id, o]) => [id, { id, ...o }]),
    ].find(([id]) => id === defaultModel.model)?.[1];
    if (!entry && !catalogRoutes.has(defaultModel.provider)) {
        problems.push(`agent-default-model names model "${defaultModel.model}", which is not declared under provider "${defaultModel.provider}"`);
    } else if (!entry && catalogRoutes.has(defaultModel.provider)) {
        const catalog = piAiProviders.getBuiltinModels(defaultModel.provider).find((m) => m.id === defaultModel.model);
        if (!catalog) problems.push(`agent-default-model names model "${defaultModel.model}", which the "${defaultModel.provider}" catalog does not list`);
        else report.push(`  default model ${defaultModel.provider}/${defaultModel.model}: effective input = ${JSON.stringify(catalog.input)}`);
    }
    if (entry) {
        const effective = effectiveInput(defaultModel.provider, defaultModel.model, entry);
        report.push(`  default model ${defaultModel.provider}/${defaultModel.model}: effective input = ${JSON.stringify(effective)}`);
        if (!effective.includes("image")) {
            problems.push(`default model ${defaultModel.provider}/${defaultModel.model} resolves to input ${JSON.stringify(effective)} without "image"; the CAD reference-image workflow cannot run`);
        }
    }
}

console.log(`settings: ${settingsPath}`);
if (report.length > 0) console.log(report.join("\n"));
if (problems.length > 0) {
    console.error(problems.map((line) => `FAIL: ${line}`).join("\n"));
    process.exit(1);
}
console.log("image capability: OK");
