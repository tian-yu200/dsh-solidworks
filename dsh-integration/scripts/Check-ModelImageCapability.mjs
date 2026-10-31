// Check-ModelImageCapability.mjs

// Validates that DSH's LLM configuration keeps image (vision) input available
// for model switching. Ground truth is the installed llm-pi-ai adapter itself:
// the settings section is parsed through its own Config schema, and each
// model's effective input modalities are resolved with the same
// declared-input -> installed-catalog -> provider-defaultInput chain the
// adapter uses at request time (a message containing an image is rejected
// unless the resolved list includes "image").

// Usage:
//   node Check-ModelImageCapability.mjs [--settings <settings.yaml>] [--profile-dump <dsh dump>] [--profiles <dsh profiles dir>]
//
// Exit codes: 0 = pass, 1 = validation failed, 2 = environment/setup error.

import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { pathToFileURL } from "node:url";

function fail(message) {
    console.error(`ERROR: ${message}`);
    process.exit(2);
}

function readArg(name, fallback) {
    const index = process.argv.indexOf(name);
    if (index === -1 || index + 1 >= process.argv.length) return fallback;
    return process.argv[index + 1];
}

const settingsPath = path.resolve(readArg("--settings", path.join(os.homedir(), ".dsh", "settings.yaml")));
const profileDumpArg = readArg("--profile-dump", "");
const profileDumpPath = profileDumpArg ? path.resolve(profileDumpArg) : "";
const configPath = profileDumpPath || settingsPath;
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

function loadConfig(file) {
  try {
    // DSH's --dump-config includes lazy !!js values in unrelated plugins. Keep those as inert
    // strings; only the already-resolved llm-pi-ai and default-model plugin configs are inspected.
    const schema = yaml.DEFAULT_SCHEMA.extend([
        new yaml.Type("tag:yaml.org,2002:js", { kind: "scalar", construct: (value) => value }),
    ]);
    return yaml.load(fs.readFileSync(file, "utf8"), { schema });
  } catch (error) {
    // Parser messages/marks can include the full source and provider credentials.
    const location = error.mark ? ` at line ${error.mark.line + 1}, column ${error.mark.column + 1}` : "";
    fail(`configuration could not be parsed${location}${error.code ? ` (${error.code})` : ""}`);
  }
}
const raw = loadConfig(configPath);
if (!raw || typeof raw !== "object") fail(`configuration is empty or invalid: ${configPath}`);

// Same layering as dsh-settings: plain objects merge; arrays/scalars replace.
function mergeSettings(base, override) {
    if (override === undefined) return base;
    const isMap = value => value && typeof value === "object" && !Array.isArray(value);
    if (!isMap(base) || !isMap(override)) return override;
    const result = { ...base };
    for (const [key, value] of Object.entries(override)) result[key] = mergeSettings(result[key], value);
    return result;
}

let adapterConfig;
let defaultModel;
if (profileDumpPath) {
    if (!Array.isArray(raw)) fail(`profile dump is not a DSH plugin list: ${configPath}`);
    adapterConfig = raw.find((plugin) => plugin?.id === "llm-pi-ai")?.config;
    defaultModel = raw.find((plugin) => plugin?.id === "agent-default-model")?.config;
    const settingsPlugin = raw.find((plugin) => plugin?.id === "settings");
    const explicitSettings = process.argv.includes("--settings");
    const overlayPath = explicitSettings ? settingsPath : path.resolve(
        settingsPlugin?.config?.path ?? path.join(
            settingsPlugin?.config?.dshHome ?? path.dirname(profilesDir), "settings.yaml"));
    if ((explicitSettings || settingsPlugin?.name === "@deepseek-ai/dsh-settings-file") && fs.existsSync(overlayPath)) {
        const settings = loadConfig(overlayPath);
        if (settings && (typeof settings !== "object" || Array.isArray(settings))) fail("runtime settings must be a namespace mapping");
        adapterConfig = mergeSettings(adapterConfig, settings?.["llm-pi-ai"]);
        defaultModel = mergeSettings(defaultModel, settings?.["agent-default-model"]);
    }
    if (!adapterConfig || !defaultModel) {
        fail("profile dump is missing llm-pi-ai or agent-default-model configuration");
    }
} else {
    adapterConfig = raw["llm-pi-ai"];
    defaultModel = raw["agent-default-model"];
}

let section;
try {
    section = llmPiAi.Config(adapterConfig);
} catch (error) {
    fail("llm-pi-ai configuration rejected by the installed adapter schema");
}

const providers = section.providers ?? {};
const selectedProvider = readArg("--provider", "");
if (selectedProvider && !providers[selectedProvider]) fail("selected provider is not configured");
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
    if (selectedProvider && name !== selectedProvider) continue;
    const isCatalog = catalogRoutes.has(name);
    const explicitDefault = adapterConfig?.providers?.[name]?.defaultInput !== undefined;
    const entries = [
        ...(provider.models ?? []).map((m) => [m.id, m]),
        ...Object.entries(provider.modelOverrides ?? {}).map(([id, o]) => [id, o]),
    ];
    if (!isCatalog && !explicitDefault &&
        (entries.length === 0 || entries.some(([, entry]) => !declaredInput(entry)))) {
        problems.push(
            `provider "${name}" has models without an explicit input list and no defaultInput; ` +
            `those models resolve to text-only, so image attachments will be refused ` +
            `(add "defaultInput: [ text, image ]" under the provider, or "[ text ]" to opt out deliberately)`
        );
    }
    if (entries.length === 0 && isCatalog) {
        report.push(`  ${name}: catalog route (${piAiProviders.getBuiltinModels(name).length} models, per-model input from the installed catalog)`);
        continue;
    }
    for (const [modelId, entry] of entries) {
        const effective = effectiveInput(name, modelId, entry);
        report.push(`  ${name}/${modelId}: effective input = ${JSON.stringify(effective)}`);
    }
}

if (selectedProvider && defaultModel?.provider !== selectedProvider) {
    report.push("  default model is outside selected provider scope");
} else if (!defaultModel?.provider || !defaultModel?.model) {
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
        else {
            report.push(`  default model ${defaultModel.provider}/${defaultModel.model}: effective input = ${JSON.stringify(catalog.input)}`);
            if (!catalog.input.includes("image")) {
                problems.push(`default model ${defaultModel.provider}/${defaultModel.model} resolves to input ${JSON.stringify(catalog.input)} without "image"; the CAD reference-image workflow cannot run`);
            }
        }
    }
    if (entry) {
        const effective = effectiveInput(defaultModel.provider, defaultModel.model, entry);
        report.push(`  default model ${defaultModel.provider}/${defaultModel.model}: effective input = ${JSON.stringify(effective)}`);
        if (!effective.includes("image")) {
            problems.push(`default model ${defaultModel.provider}/${defaultModel.model} resolves to input ${JSON.stringify(effective)} without "image"; the CAD reference-image workflow cannot run`);
        }
    }
}

console.log(`${profileDumpPath ? "profile config" : "settings"}: ${configPath}`);
if (report.length > 0) console.log(report.join("\n"));
if (problems.length > 0) {
    console.error(problems.map((line) => `FAIL: ${line}`).join("\n"));
    process.exit(1);
}
console.log(`image capability: OK${selectedProvider ? ` (provider ${selectedProvider})` : ""}`);
