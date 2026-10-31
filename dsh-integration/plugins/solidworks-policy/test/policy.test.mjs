// Behaviour tests for the solidworks-policy plugin, run against a stub of the DSH tool runtime.
//   node --test dsh-integration/plugins/solidworks-policy/test/
// The stub reproduces the one core rule these tests exist for: tools.restrict() rejects a filter
// that names a global tool the registry does not have. A profile that ships a different tool set
// than the allowlist assumes (the desktop has no modlens) must therefore still get a restriction.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

// Diagnostics go to <DSH_HOME>/logs; keep them out of the real profile.
const DSH_HOME = fs.mkdtempSync(path.join(os.tmpdir(), "sw-policy-test-"));
process.env.DSH_HOME = DSH_HOME;

const { ALLOWED_SKILLS, ALLOWED_TOOLS, SOLIDWORKS_TOOLS, SUPPORT_TOOLS, apply, filterSkillCatalog } =
  await import("../index.js");

const CORE_TOOLS = [
  "bash",
  "read_file",
  "write_file",
  "edit_file",
  "fetch",
  "browser_navigate",
  "playwright_click",
  "chrome_devtools_screenshot",
  "github_create_pull_request",
];

/**
 * A minimal `ctx` plus the `tools` service an agent carries.
 * @param known - tool names this composition registers; everything else reads as absent.
 */
function harness(known = [...ALLOWED_TOOLS, ...CORE_TOOLS], { scoped = false } = {}) {
  const registered = new Set(known);
  const guards = [];
  const restrictions = [];
  const scopeRestrictions = [];
  const warnings = [];
  const listeners = new Map();
  const tools = {
    guard(guard) {
      guards.push(guard);
      return () => {};
    },
    restrict(filter) {
      return checkedRestrict(filter, restrictions);
    },
    get(name) {
      return registered.has(name) ? { name } : undefined;
    },
  };
  function checkedRestrict(filter, into) {
    const named = [...(filter.allow ?? []), ...(filter.deny ?? [])];
    if (filter.allow === void 0 && filter.deny === void 0) {
      throw new Error("tools.restrict({}) is a no-op: pass `allow` and/or `deny`");
    }
    if (named.includes("run_code")) {
      throw new Error('tools.restrict() cannot name reserved PTC mode presentation transport "run_code"');
    }
    const unknown = named.filter((toolName) => !registered.has(toolName));
    if (unknown.length) {
      // Verbatim shape of the core's message (dsh-tools 0.1.1 and 0.2.0 agree on it), because the
      // plugin recovers the restrictable set from "known global tools: …".
      throw new Error(
        `tools.restrict() names unknown global tool${unknown.length > 1 ? "s" : ""} ` +
          `${unknown.map((n) => `"${n}"`).join(", ")}; known global tools: ` +
          `${[...registered].sort().join(", ") || "(none)"}`,
      );
    }
    into.push(filter);
    return () => {};
  }
  // The plugin's own context: a preset-scoped row on the desktop core, a host-level row on the
  // web profile, where the core refuses a restriction outright.
  const scopeTools = {
    guard: tools.guard,
    get: tools.get,
    restrict(filter) {
      if (!scoped) {
        throw new Error("tools.restrict() requires a scoped context (agent.ctx): a context-global " +
          "restriction would mask every agent — deny the tool for the intended agent instead");
      }
      return checkedRestrict(filter, scopeRestrictions);
    },
  };
  const ctx = {
    tools: scopeTools,
    logger: { warn: (message) => warnings.push(message) },
    on(event, handler, options) {
      if (!listeners.has(event)) listeners.set(event, []);
      // cordis: a prepended listener runs first, i.e. outermost in a waterfall.
      const prepend = options === true || options?.prepend === true;
      listeners.get(event)[prepend ? "unshift" : "push"](handler);
      return () => {};
    },
  };
  return {
    ctx,
    guards,
    registered,
    restrictions,
    scopeRestrictions,
    warnings,
    agent: { ctx: { tools } },
    emit(event, payload) {
      for (const handler of listeners.get(event) ?? []) handler(payload, () => {});
    },
    // cordis waterfall: listeners in list order, each calling next() for the rest, then `inner`.
    waterfall(event, payload, inner) {
      const queue = [...(listeners.get(event) ?? [])];
      const next = () => (queue.shift() ?? inner)(payload, next);
      return next();
    },
    preExecute(exec, next = async () => ({ kind: "allow" })) {
      return listeners.get("tools/pre-execute")[0].call(tools, exec, next);
    },
    result(exec, result) {
      for (const handler of listeners.get("tools/result") ?? []) handler(exec, result);
    },
    execute(exec, next = async () => ({ kind: "allow" })) {
      const handler = listeners.get("tools/execute")?.[0];
      return handler ? handler.call(tools, exec, next) : next();
    },
  };
}

function fireAgentCreated(known) {
  const env = known === void 0 ? harness() : harness(known);
  apply(env.ctx);
  env.emit("agent/created", { agent: env.agent });
  return env;
}

// --- the catalog restriction -------------------------------------------------

test("every allowed tool is restricted in when the profile has all of them", () => {
  const env = fireAgentCreated();
  assert.equal(env.restrictions.length, 1);
  assert.deepEqual([...env.restrictions[0].allow].sort(), [...ALLOWED_TOOLS].sort());
  assert.deepEqual(env.warnings, []);
});

test("a profile without modlens still gets the restriction instead of a throw", () => {
  // The desktop (tauri) profile installs no @liustack/modlens, so modlens_read_image is not a
  // registered tool there. Leaving the failed call in place would abort agent/created — which the
  // core documents as failing agent creation — and skipping restrict() would leave the model
  // holding all 176 desktop tools, which is the failure this plugin exists to prevent.
  const env = fireAgentCreated(
    [...ALLOWED_TOOLS.filter((n) => n !== "modlens_read_image"), ...CORE_TOOLS],
  );
  assert.equal(env.restrictions.length, 1);
  assert.equal(env.restrictions[0].allow.includes("modlens_read_image"), false);
  assert.equal(env.restrictions[0].allow.includes("mcp__solidworks__start_job"), true);
  assert.match(env.warnings.join("\n"), /modlens_read_image/);
});

test("the preset-scoped skill tool survives a partial-catalog retry", () => {
  // `skill` is registered by dsh-tool-skill INSIDE the agent preset's scope, so it is restrictable
  // for the agent but absent from the global view. A plugin that asked tools.get() about it would
  // call it unknown and restrict() would then deny the tool the persona's first rule needs.
  const env = fireAgentCreated(
    [...ALLOWED_TOOLS.filter((n) => n !== "modlens_read_image"), ...CORE_TOOLS],
  );
  for (const toolName of [
    "skill",
    "ask_user_question",
    "todo_write",
    "read_mcp_resource",
  ]) {
    assert.ok(env.restrictions[0].allow.includes(toolName), `${toolName} must stay allowed`);
  }
  for (const toolName of SOLIDWORKS_TOOLS) {
    assert.ok(env.restrictions[0].allow.includes(toolName), `${toolName} must stay allowed`);
  }
});

test("the allowlist is never widened by the retry", () => {
  // A core that reports the whole catalog (including bash and the browsers) must still yield only
  // the allowed subset — restrict() is an allowlist, not "everything the scope knows".
  const env = fireAgentCreated(
    [...ALLOWED_TOOLS.filter((n) => n !== "modlens_read_image"), ...CORE_TOOLS],
  );
  assert.deepEqual(
    [...env.restrictions[0].allow].sort(),
    [...ALLOWED_TOOLS.filter((n) => n !== "modlens_read_image")].sort(),
  );
  for (const toolName of CORE_TOOLS) {
    assert.equal(env.restrictions[0].allow.includes(toolName), false);
  }
});

test("an error the plugin cannot read leaves restrict() alone and says so", () => {
  const env = harness([...ALLOWED_TOOLS, ...CORE_TOOLS]);
  env.agent.ctx.tools.restrict = () => {
    throw new Error("tools.restrict() cannot name reserved PTC mode presentation transport");
  };
  apply(env.ctx);
  env.emit("agent/created", { agent: env.agent });
  assert.match(env.warnings.join("\n"), /could not read the restrictable tool set/);
});

test("nothing is restricted when the scope has none of the allowed tools", () => {
  // restrict({ allow: [] }) would deny every inherited tool, including a CAD catalog that simply
  // has not connected yet; the guard is what refuses calls, so skipping the call is the safe half.
  const env = fireAgentCreated(CORE_TOOLS);
  assert.deepEqual(env.restrictions, []);
});

test("restriction never depends on tools.get()", () => {
  // Regression pin for the global-view bug: a scope that reports every tool as unknown to get()
  // must still be restricted, because get() answers for the global view and not for this agent.
  const env = harness([...ALLOWED_TOOLS, ...CORE_TOOLS]);
  env.agent.ctx.tools.get = () => undefined;
  apply(env.ctx);
  env.emit("agent/created", { agent: env.agent });
  assert.equal(env.restrictions.length, 1);
  assert.deepEqual([...env.restrictions[0].allow].sort(), [...ALLOWED_TOOLS].sort());
});

// --- the preset-scope restriction (desktop core) --------------------------------

const DESKTOP_KNOWN = [...ALLOWED_TOOLS.filter((n) => n !== "modlens_read_image"), ...CORE_TOOLS];

test("the desktop sees exactly the sixteen tools it needs", () => {
  // Measured 2026-10-31: the desktop request carried 177 tool schemas (~45k tokens) of which a
  // whole modelling session used 11; August's carried 18. modlens is not installed there.
  assert.equal(ALLOWED_TOOLS.filter((n) => n !== "modlens_read_image").length, 16);
  assert.equal(ALLOWED_TOOLS.includes("list_mcp_resources"), false);
});

test("a preset-scoped row denies every inherited tool off the allowlist as soon as it applies", () => {
  const env = harness(DESKTOP_KNOWN, { scoped: true });
  apply(env.ctx);
  assert.equal(env.scopeRestrictions.length, 1);
  assert.equal(env.scopeRestrictions[0].allow, undefined, "an allowlist would have to name the preset's own tools");
  assert.deepEqual([...env.scopeRestrictions[0].deny].sort(), [...CORE_TOOLS].sort());
  for (const toolName of ALLOWED_TOOLS) {
    assert.equal(env.scopeRestrictions[0].deny.includes(toolName), false, `${toolName} must never be denied`);
  }
});

test("a late MCP registration is denied on tools/change, once", () => {
  const env = harness(DESKTOP_KNOWN, { scoped: true });
  apply(env.ctx);
  env.registered.add("mcp__laya__laya_route");
  env.registered.add("mcp__github__issue_write");
  env.registered.add("list_mcp_resources");
  env.emit("tools/change");
  assert.equal(env.scopeRestrictions.length, 2);
  assert.deepEqual([...env.scopeRestrictions[1].deny].sort(),
    ["list_mcp_resources", "mcp__github__issue_write", "mcp__laya__laya_route"]);
  env.emit("tools/change");
  env.emit("agent/pre-step", { agent: env.agent, turn: 1 });
  assert.equal(env.scopeRestrictions.length, 2, "nothing new, nothing restricted again");
});

test("SolidWorks tools that connect late are never denied", () => {
  const env = harness(CORE_TOOLS, { scoped: true });
  apply(env.ctx);
  for (const toolName of SOLIDWORKS_TOOLS) env.registered.add(toolName);
  env.emit("tools/change");
  for (const filter of env.scopeRestrictions) {
    for (const toolName of SOLIDWORKS_TOOLS) assert.equal(filter.deny.includes(toolName), false);
  }
});

test("a host-level row leaves the scope alone and still restricts each agent", () => {
  const env = harness(DESKTOP_KNOWN);
  apply(env.ctx);
  env.emit("tools/change");
  env.emit("agent/created", { agent: env.agent });
  assert.deepEqual(env.scopeRestrictions, []);
  assert.equal(env.restrictions.length, 1);
  assert.equal(env.restrictions[0].allow.includes("mcp__solidworks__start_job"), true);
});

test("the restriction outcome is written to the diagnostics log", () => {
  const env = harness(DESKTOP_KNOWN, { scoped: true });
  apply(env.ctx);
  const log = fs.readFileSync(path.join(DSH_HOME, "logs", "solidworks-policy.log"), "utf8");
  assert.match(log, /"event":"scope-restricted"/);
  assert.match(log, /"newly_denied":9/);
});

// --- the skill catalog ----------------------------------------------------------

// The exact frame dsh-tool-skill's renderCatalogMessage produces (desktop core, 2026-10-31).
function catalogMessage(names) {
  const entries = names.map((n) => ({ name: n, description: `${n} description` }));
  return {
    id: "catalog-1",
    role: "user",
    content: [{
      type: "text",
      text: [
        "<system-reminder>",
        "A skill is a reusable set of task-specific instructions. The following skills are available in this session:",
        "",
        "<available_skills>",
        ...entries.map((e) => `- \`${e.name}\`: ${e.description}`),
        "</available_skills>",
        "",
        "If the user names a skill, or the task clearly matches a skill's description, call the `skill` tool with the exact skill name before taking task actions.",
        "</system-reminder>",
      ].join("\n"),
    }],
    source: { kind: "skill-catalog", form: "catalog", entries },
  };
}

const MANY_SKILLS = ["academic-research-suite", "cad", "dxf", "solidworks-cad", "step-parts", "xhs"];

test("the catalog text keeps only the SolidWorks skill and its framing", () => {
  const decision = { kind: "enter", messages: [{ id: "u1", role: "user", content: [], source: { kind: "user" } }, catalogMessage(MANY_SKILLS)] };
  const out = filterSkillCatalog(decision, () => {});
  const text = out.messages[1].content[0].text;
  assert.deepEqual(ALLOWED_SKILLS, ["solidworks-cad"]);
  assert.match(text, /- `solidworks-cad`: solidworks-cad description/);
  for (const other of MANY_SKILLS.filter((n) => n !== "solidworks-cad")) {
    assert.doesNotMatch(text, new RegExp("`" + other + "`"), `${other} must be gone`);
  }
  assert.match(text, /^<system-reminder>\n/);
  assert.match(text, /<\/available_skills>\n\nIf the user names a skill/);
  assert.equal(out.messages[0], decision.messages[0], "other messages are untouched");
});

test("the catalog's durable entries stay complete so it is not republished every step", () => {
  const original = catalogMessage(MANY_SKILLS);
  const out = filterSkillCatalog({ kind: "enter", messages: [original] }, () => {});
  assert.equal(out.messages[0].id, original.id);
  assert.deepEqual(out.messages[0].source, original.source);
  assert.equal(out.messages[0].source.entries.length, MANY_SKILLS.length);
});

test("the catalog is left alone when filtering would remove the SolidWorks skill too", () => {
  const decision = { kind: "enter", messages: [catalogMessage(["cad", "dxf"])] };
  assert.equal(filterSkillCatalog(decision, () => {}), decision);
  const only = { kind: "enter", messages: [catalogMessage(["solidworks-cad"])] };
  assert.equal(filterSkillCatalog(only, () => {}), only);
  const rejected = { kind: "reject" };
  assert.equal(filterSkillCatalog(rejected, () => {}), rejected);
});

test("the filter wraps dsh-tool-skill's pre-step even though that plugin registers first", async () => {
  const env = harness(DESKTOP_KNOWN, { scoped: true });
  // dsh-tool-skill sits earlier in the preset, so its listener is registered before the policy's.
  env.ctx.on("agent/pre-step", async (_event, next) => {
    const decision = await next();
    return { ...decision, messages: [...decision.messages, catalogMessage(MANY_SKILLS)] };
  });
  apply(env.ctx);
  const decision = await env.waterfall("agent/pre-step", { agent: env.agent, turn: 1 },
    async () => ({ kind: "enter", messages: [] }));
  const text = decision.messages.at(-1).content[0].text;
  assert.match(text, /`solidworks-cad`/);
  assert.doesNotMatch(text, /`cad`|`step-parts`|`xhs`/);
  const log = fs.readFileSync(path.join(DSH_HOME, "logs", "solidworks-policy.log"), "utf8");
  assert.match(log, /"event":"skill-catalog-filtered","kept":1,"dropped":5/);
});

test("the skill tool loads only the SolidWorks skill", async () => {
  const env = harness();
  apply(env.ctx);
  assert.equal((await env.preExecute({ name: "skill", arguments: { name: "solidworks-cad" } })).kind, "allow");
  for (const other of ["cad", "step-parts", "academic-research-suite", ""]) {
    const decision = await env.preExecute({ name: "skill", arguments: { name: other } });
    assert.equal(decision.kind, "deny", `skill ${JSON.stringify(other)} must be refused`);
    assert.match(decision.reason, /Only the solidworks-cad skill/);
  }
});

// --- the guard ---------------------------------------------------------------

test("the guard admits exactly the allowlist", () => {
  const env = harness();
  apply(env.ctx);
  const guard = env.guards[0];
  for (const toolName of ALLOWED_TOOLS) {
    assert.equal(guard({ name: toolName }), undefined, `${toolName} must pass the guard`);
  }
  for (const toolName of CORE_TOOLS) {
    assert.match(String(guard({ name: toolName })), /unavailable in the SolidWorks profile/);
  }
  for (const toolName of ["Read", "Bash", "Grep", "WebFetch", "mcp__playwright__navigate"]) {
    assert.ok(guard({ name: toolName }), `${toolName} must be denied`);
  }
});

test("the allowlist is the twelve CAD tools plus the support tools, with no duplicates", () => {
  assert.equal(SOLIDWORKS_TOOLS.length, 12);
  assert.ok(SOLIDWORKS_TOOLS.every((toolName) => toolName.startsWith("mcp__solidworks__")));
  assert.deepEqual(
    [...ALLOWED_TOOLS],
    [...SOLIDWORKS_TOOLS, ...SUPPORT_TOOLS],
    "ALLOWED_TOOLS must be exactly the CAD tools followed by the support tools",
  );
  assert.equal(new Set(ALLOWED_TOOLS).size, ALLOWED_TOOLS.length);
  for (const toolName of SUPPORT_TOOLS) {
    assert.match(toolName, /^[a-z_]+$/, "support tools are model-visible names, lowercase");
  }
});

// --- pre-execute ------------------------------------------------------------

test("confirm_action becomes a user prompt carrying its own summary", async () => {
  const env = harness();
  apply(env.ctx);
  const asked = await env.preExecute({
    name: "mcp__solidworks__confirm_action",
    arguments: { action_summary: "Overwrite Bracket.SLDPRT" },
  });
  assert.equal(asked.kind, "ask");
  assert.equal(asked.reason, "Overwrite Bracket.SLDPRT");

  const blank = await env.preExecute({ name: "mcp__solidworks__confirm_action", arguments: {} });
  assert.equal(blank.reason, "Confirm the pending SolidWorks CAD action.");

  const refused = await env.preExecute(
    { name: "mcp__solidworks__confirm_action", arguments: {} },
    async () => ({ kind: "deny", reason: "already denied" }),
  );
  assert.equal(refused.kind, "deny", "a denial from next() is not turned into an ask");
});

test("a pending user question blocks SolidWorks calls until its tool settles", async () => {
  const env = harness();
  apply(env.ctx);
  const agent = {};
  const ask = { name: "ask_user_question", agent, arguments: { questions: [] } };
  const cad = { name: "mcp__solidworks__inspect_state", agent, arguments: {} };

  assert.equal((await env.preExecute(ask)).kind, "allow");
  const blocked = await env.preExecute(cad);
  assert.equal(blocked.kind, "deny");
  assert.match(blocked.reason, /ask_user_question is waiting/);

  await env.execute(ask);
  assert.equal((await env.preExecute(cad)).kind, "allow");
});

test("overlapping user questions keep SolidWorks calls blocked until all settle", async () => {
  const env = harness();
  apply(env.ctx);
  const agent = {};
  const first = { name: "ask_user_question", agent, arguments: { questions: [] } };
  const second = { name: "ask_user_question", agent, arguments: { questions: [] } };
  const cad = { name: "mcp__solidworks__inspect_state", agent, arguments: {} };

  assert.equal((await env.preExecute(first)).kind, "allow");
  assert.equal((await env.preExecute(second)).kind, "allow");
  await env.execute(first);
  assert.equal((await env.preExecute(cad)).kind, "deny");
  await env.execute(second);
  assert.equal((await env.preExecute(cad)).kind, "allow");
});

test("a question finalized without dispatch releases its pending gate", async () => {
  const env = harness();
  apply(env.ctx);
  const agent = {};
  const ask = { name: "ask_user_question", agent, arguments: {} };
  const cad = { name: "mcp__solidworks__inspect_state", agent, arguments: {} };
  await env.preExecute(ask);
  env.emit("tools/result", ask); // A later guard or cancellation skipped tools/execute.
  assert.equal((await env.preExecute(cad)).kind, "allow");
});

test("execute and final-result cleanup cannot release a second pending question", async () => {
  const env = harness();
  apply(env.ctx);
  const agent = {};
  const first = { name: "ask_user_question", agent, arguments: {} };
  const second = { name: "ask_user_question", agent, arguments: {} };
  const cad = { name: "mcp__solidworks__inspect_state", agent, arguments: {} };
  await env.preExecute(first);
  await env.preExecute(second);
  await env.execute(first);
  env.emit("tools/result", first);
  assert.equal((await env.preExecute(cad)).kind, "deny");
  env.emit("tools/result", second);
  assert.equal((await env.preExecute(cad)).kind, "allow");
});

test("a synchronous pre-execute error does not leave a pending question", async () => {
  const env = harness();
  apply(env.ctx);
  const agent = {};
  const ask = { name: "ask_user_question", agent, arguments: {} };
  await assert.rejects(async () => env.preExecute(ask, () => { throw new Error("fixture failure"); }));
  const cad = { name: "mcp__solidworks__inspect_state", agent, arguments: {} };
  assert.equal((await env.preExecute(cad)).kind, "allow");
});

test("a question awaiting permission keeps the gate until its final result", async () => {
  const env = harness();
  apply(env.ctx);
  const agent = {};
  const ask = { name: "ask_user_question", agent, arguments: {} };
  const cad = { name: "mcp__solidworks__inspect_state", agent, arguments: {} };
  await env.preExecute(ask, async () => ({ kind: "ask", reason: "permission" }));
  assert.equal((await env.preExecute(cad)).kind, "deny");
  env.emit("tools/result", ask);
  assert.equal((await env.preExecute(cad)).kind, "allow");
});

test("image readers reject URLs, accept local paths, and keep the tool allowlist", async () => {
  const env = harness();
  apply(env.ctx);
  let reached = 0;
  const next = async () => {
    reached += 1;
    return { kind: "allow" };
  };
  assert.equal((await env.preExecute({ name: "bash", arguments: {} }, next)).kind, "deny");
  assert.equal(reached, 0, "a denied tool must not be dispatched to next()");

  for (const url of ["http://x/y.png", "file:///C:/a.png", "https://x/y.png", "ftp://h/a.png"]) {
    for (const [toolName, key] of [
      ["modlens_read_image", "path"],
      ["mcp__solidworks__read_drawing_region", "image_path"],
    ]) {
      const decision = await env.preExecute({ name: toolName, arguments: { [key]: url } }, next);
      assert.equal(decision.kind, "deny", `${toolName} must refuse ${url}`);
      assert.match(decision.reason, /local image (file )?path/);
    }
  }
  const ok = await env.preExecute(
    { name: "mcp__solidworks__read_drawing_region", arguments: { image_path: "D:/draw/a.png" } },
    next,
  );
  assert.equal(ok.kind, "allow");
  assert.equal(reached, 1);
});


test('OCR and crops accept valid attachment IDs but modlens remains local-only', async () => {
  const env = harness(); apply(env.ctx);
  const digest = 'sha256:' + 'a'.repeat(64);
  for (const name of ['mcp__solidworks__read_drawing_text', 'mcp__solidworks__read_drawing_region']) {
    assert.equal((await env.preExecute({ name, arguments: { image_path: digest } })).kind, 'allow');
    for (const image_path of ['sha256:bad', 'sha256:' + 'g'.repeat(64), ' file:/a.png', 'https://x/y.png']) {
      const decision = await env.preExecute({ name, arguments: { image_path } });
      assert.equal(decision.kind, 'deny');
      assert.match(decision.reason, /do not guess/);
    }
  }
  assert.equal((await env.preExecute({ name: 'modlens_read_image', arguments: { path: digest } })).kind, 'deny');
});


test('host pre-step and final result connect task isolation to the monotonic guard', async () => {
  const env = harness(); apply(env.ctx);
  // Stub the host waterfall emitter with its actual (payload, next) shape.
  const next = async () => ({ kind: 'allow' });
  env.emit('agent/pre-step', { agent: env.agent, turn: 1 });
  const exec = { agent: env.agent, name: 'mcp__solidworks__read_drawing_region', arguments: { image_path: 'missing.png' } };
  for (let i = 0; i < 3; i++) env.result(exec, { isError: true, content: [] });
  assert.match(env.guards[0](exec), /INTAKE_NO_PROGRESS/);
  env.emit('agent/pre-step', { agent: env.agent, turn: 2 });
  assert.equal(env.guards[0](exec), undefined);
  env.emit('agent/disposed', { agent: env.agent });
  assert.equal(env.guards[0](exec), undefined);
});

// --- reasoning effort per step (user decision 2026-10-31: auto switching) ------------------------

function effortEnv({ supportsMedium = true } = {}) {
  const env = harness(DESKTOP_KNOWN, { scoped: true });
  env.ctx.get = (service) => service === "llm" ? {
    resolveCallConfig: async (config) => {
      if (config.reasoningEffort === "medium" && !supportsMedium) throw new Error("UNSUPPORTED_REASONING_EFFORT");
      return config;
    },
  } : undefined;
  apply(env.ctx);
  let header = { provider: "gpt", model: "gpt-6.1-sol", reasoningEffort: "high" };
  // The core seeds each step from the last logged header, and logs a header whenever it changes.
  env.request = async (turn) => {
    const config = await env.waterfall("agent/request", { agent: env.agent, turn }, async () => ({ ...header }));
    header = config;
    return config.reasoningEffort;
  };
  env.chooseEffort = (effort) => { header = { ...header, reasoningEffort: effort }; };
  env.cadResult = (name, envelope) => env.result({ name, agent: env.agent, arguments: {} },
    { content: [{ type: "text", text: JSON.stringify(envelope) }] });
  return env;
}

const BUILT = { status: "completed", completed_steps: ["plate", "sleeve"] };

test("reading and graph writing keep the chosen effort; steps after the build drop to medium", async () => {
  const env = effortEnv();
  assert.equal(await env.request(1), "high");
  env.cadResult("mcp__solidworks__start_job", { status: "completed", completed_steps: ["ensure_ready"] });
  assert.equal(await env.request(1), "high", "start_job is not a build");
  env.cadResult("mcp__solidworks__submit_feature_graph", BUILT);
  assert.equal(await env.request(1), "medium");
  env.cadResult("mcp__solidworks__inspect_state", { status: "completed" });
  assert.equal(await env.request(1), "medium");
  const log = fs.readFileSync(path.join(DSH_HOME, "logs", "solidworks-policy.log"), "utf8");
  assert.match(log, /"event":"effort-lowered","from":"high","to":"medium"/);
});

test("a failed SolidWorks result and a new turn both return to the chosen effort", async () => {
  const env = effortEnv();
  await env.request(1);
  env.cadResult("mcp__solidworks__submit_feature_graph", BUILT);
  assert.equal(await env.request(1), "medium");
  env.cadResult("mcp__solidworks__submit_feature_graph", { status: "failed", completed_steps: [] });
  assert.equal(await env.request(1), "high", "a repair is reasoning work");
  env.cadResult("mcp__solidworks__submit_feature_graph", BUILT);
  assert.equal(await env.request(1), "medium");
  assert.equal(await env.request(2), "high", "a new user turn may ask for design changes");
});

test("a model without medium, or a user who chose medium, is left alone", async () => {
  const unsupported = effortEnv({ supportsMedium: false });
  await unsupported.request(1);
  unsupported.cadResult("mcp__solidworks__submit_feature_graph", BUILT);
  assert.equal(await unsupported.request(1), "high");
  const modest = effortEnv();
  modest.chooseEffort("medium");
  assert.equal(await modest.request(1), "medium");
  modest.cadResult("mcp__solidworks__submit_feature_graph", BUILT);
  assert.equal(await modest.request(1), "medium");
  assert.equal(await modest.request(2), "medium", "never raised above what the user chose");
});
