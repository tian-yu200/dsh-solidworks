// Behaviour tests for the solidworks-policy plugin, run against a stub of the DSH tool runtime.
//   node --test dsh-integration/plugins/solidworks-policy/test/
// The stub reproduces the one core rule these tests exist for: tools.restrict() rejects a filter
// that names a global tool the registry does not have. A profile that ships a different tool set
// than the allowlist assumes (the desktop has no modlens) must therefore still get a restriction.
import assert from "node:assert/strict";
import { test } from "node:test";

import { ALLOWED_TOOLS, SOLIDWORKS_TOOLS, SUPPORT_TOOLS, apply } from "../index.js";

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
function harness(known = [...ALLOWED_TOOLS, ...CORE_TOOLS]) {
  const registered = new Set(known);
  const guards = [];
  const restrictions = [];
  const warnings = [];
  const listeners = new Map();
  const tools = {
    guard(guard) {
      guards.push(guard);
      return () => {};
    },
    restrict(filter) {
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
      restrictions.push(filter);
      return () => {};
    },
    get(name) {
      return registered.has(name) ? { name } : undefined;
    },
  };
  const ctx = {
    tools,
    logger: { warn: (message) => warnings.push(message) },
    on(event, handler) {
      if (!listeners.has(event)) listeners.set(event, []);
      listeners.get(event).push(handler);
      return () => {};
    },
  };
  return {
    ctx,
    guards,
    restrictions,
    warnings,
    agent: { ctx: { tools } },
    emit(event, payload) {
      for (const handler of listeners.get(event) ?? []) handler(payload, () => {});
    },
    preExecute(exec, next = async () => ({ kind: "allow" })) {
      return listeners.get("tools/pre-execute")[0].call(tools, exec, next);
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
    "list_mcp_resources",
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

test("the allowlist is the eleven CAD tools plus the support tools, with no duplicates", () => {
  assert.equal(SOLIDWORKS_TOOLS.length, 11);
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

test("image tools take a local path only, and a foreign tool never reaches next()", async () => {
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
      assert.match(decision.reason, /local image file path/);
    }
  }
  const ok = await env.preExecute(
    { name: "mcp__solidworks__read_drawing_region", arguments: { image_path: "D:/draw/a.png" } },
    next,
  );
  assert.equal(ok.kind, "allow");
  assert.equal(reached, 1);
});
