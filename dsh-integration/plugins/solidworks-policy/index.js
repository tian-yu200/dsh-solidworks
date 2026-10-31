import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createIntakeProgress, IMAGE_READ_TOOLS } from './intake-progress.js';

const name = "solidworks-policy";
const inject = ["tools"];

const SOLIDWORKS_TOOLS = [
  "mcp__solidworks__start_job",
  "mcp__solidworks__inspect_state",
  "mcp__solidworks__submit_feature_graph",
  "mcp__solidworks__apply_document_edits",
  "mcp__solidworks__drawing_workflow",
  "mcp__solidworks__save_or_export",
  "mcp__solidworks__request_confirmation",
  "mcp__solidworks__confirm_action",
  "mcp__solidworks__cancel_action",
  "mcp__solidworks__finish_job",
  "mcp__solidworks__read_drawing_text",
  "mcp__solidworks__read_drawing_region",
];

const SUPPORT_TOOLS = [
  "ask_user_question",
  "todo_write",
  "skill",
  "modlens_read_image",
  // The skill's references are served as resources by the solidworks MCP server, and this preset
  // has no file tool — without it the "read the reference" instruction in SKILL.md would itself be
  // refused. SKILL.md names every reference URI, so the roster tool (list_mcp_resources) is not
  // needed: measured 2026-10-31, the one call a session made to it cost a 135 s model step.
  "read_mcp_resource",
];

const ALLOWED_TOOLS = Object.freeze([
  ...SOLIDWORKS_TOOLS,
  ...SUPPORT_TOOLS,
]);
const ALLOWED_SET = new Set(ALLOWED_TOOLS);
// The one skill a SolidWorks session loads. Measured 2026-10-31: every session also received a
// 164-skill catalog (42k characters) from the global skill roots — pentest, reverse engineering,
// social media, and generic `cad`/`step-parts`/`dxf` skills that compete with this one.
const ALLOWED_SKILLS = Object.freeze(["solidworks-cad"]);
const ALLOWED_SKILL_SET = new Set(ALLOWED_SKILLS);
const SKILL_TOOL = "skill";
const CONFIRM_TOOL = "mcp__solidworks__confirm_action";
const MODLENS_TOOL = "modlens_read_image";
const REGION_TOOL = "mcp__solidworks__read_drawing_region";
const QUESTION_TOOL = "ask_user_question";
const CAD_TOOL_PREFIX = "mcp__solidworks__";
const QUESTION_WAIT_REASON =
  "ask_user_question is waiting for the user. Do not call a SolidWorks tool until it returns " +
  "the user's answer.";
const pendingUserQuestions = new WeakMap();
const MODLENS_LOCAL_ONLY_REASON =
  "modlens_read_image accepts only a non-empty local image file path in the SolidWorks " +
  "profile; URL forms such as http://, https://, ftp:// and file:// are not allowed.";
const REGION_LOCAL_ONLY_REASON =
  "Drawing readers accept a local image path or sha256:<64 hexadecimal characters> in the current DSH home. " +
  "Malformed attachment IDs and URLs (including file://) are refused; do not guess a local path.";

function beginUserQuestion(exec) {
  let pending = pendingUserQuestions.get(exec.agent);
  if (!pending) pendingUserQuestions.set(exec.agent, (pending = new Set()));
  pending.add(exec);
}

function endUserQuestion(exec) {
  const pending = pendingUserQuestions.get(exec.agent);
  if (!pending) return;
  pending.delete(exec);
  if (pending.size === 0) pendingUserQuestions.delete(exec.agent);
}

function denialReason(toolName) {
  return (
    `Tool \`${toolName}\` is unavailable in the SolidWorks profile. ` +
    "Use the twelve high-level SolidWorks MCP tools or the approved support tools."
  );
}

function isLocalImagePath(value) {
  return (
    typeof value === "string" &&
    value.trim() !== "" &&
    !/^[a-z][a-z0-9+.-]*:\/\//i.test(value.trim()) &&
    !/^(file|sha256):/i.test(value.trim())
  );
}

function isDrawingSource(value) {
  return typeof value === "string" && (/^sha256:[0-9a-f]{64}$/i.test(value.trim()) || isLocalImagePath(value));
}

/**
 * The restrictable tool set for one agent scope, as the core itself reports it.
 *
 * `tools.restrict()` refuses a filter naming anything outside that scope's inherited registry, and
 * it says so with the whole set: "…names unknown global tool "x"…; known global tools: a, b, c".
 * No public read-only call gives a plugin that set — `tools.get(name)` and `tools.schemas()` both
 * answer for the GLOBAL view unless a scope key is passed, and a scope key is not something a plugin
 * can obtain (`scopeOf` lives in `@deepseek-ai/dsh-scope`, which this plugin does not depend on).
 * That distinction is not academic: `skill` is registered by `dsh-tool-skill` inside the agent
 * preset's own scope, so a global-view check would call it unknown and restrict() would then deny
 * the very tool the persona's first rule requires.
 * @returns the allowed names this scope can be restricted to, or null when the report is unreadable.
 */
function restrictableFromError(message) {
  const known = restrictableFromMessage(message);
  if (!known || !known.length) return null;
  return ALLOWED_TOOLS.filter((toolName) => known.includes(toolName));
}

function restrictAgent(agent, logger, diag = () => {}) {
  const tools = agent.ctx.tools;
  try {
    tools.restrict({ allow: ALLOWED_TOOLS });
    diag("agent-restricted", { allowed: ALLOWED_TOOLS.length });
    return;
  } catch (error) {
    const allow = restrictableFromError(error && error.message);
    if (!allow) {
      diag("agent-restrict-unreadable", { message: String(error && error.message).slice(0, 240) });
      logger?.warn?.(
        `${name}: could not read the restrictable tool set from ${JSON.stringify(
          String(error && error.message),
        )}; leaving the catalog unrestricted (the guard still refuses everything off the allowlist)`,
      );
      return;
    }
    if (!allow.length) return;
    const dropped = ALLOWED_TOOLS.filter((toolName) => !allow.includes(toolName));
    logger?.warn?.(
      `${name}: restricting without ${dropped.join(", ")} — this composition has no such tool`,
    );
    try {
      tools.restrict({ allow });
      diag("agent-restricted", { allowed: allow.length, dropped });
    } catch (retryError) {
      diag("agent-restrict-failed", { message: String(retryError && retryError.message).slice(0, 240) });
    }
  }
}

/**
 * Append one diagnostic line to <DSH home>/logs/solidworks-policy.log, once per distinct line.
 * The desktop core persists no plugin logger output, which is how the per-agent restriction could
 * fail unseen while every request carried 177 tool schemas (~45k tokens, measured 2026-10-31).
 */
function createDiagnostics() {
  const seen = new Set();
  const file = path.join(process.env.DSH_HOME || path.join(os.homedir(), ".dsh"), "logs",
    "solidworks-policy.log");
  return (event, details = {}) => {
    const line = JSON.stringify({ event, ...details });
    if (seen.has(line)) return;
    seen.add(line);
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.appendFileSync(file, `${new Date().toISOString()} ${line}\n`, "utf8");
    } catch {
      // Diagnostics must never break the policy.
    }
  };
}

const SCOPE_PROBE = "__solidworks_policy_scope_probe__";

/**
 * Keep the preset's own scope down to the allowlist, independent of any agent event.
 *
 * On the desktop core this plugin is mounted inside the SolidWorks agent preset, whose scope is
 * the parent of every agent that preset composes. Its guard is proven to reach those agents (a
 * 2026-10-30 session shows its denial), and a restriction registered here sits on the very layer
 * chain the guard does; the per-agent restriction on agent/created never took effect there.
 *
 * A restriction may only name tools this scope inherits, and the preset's own registrations
 * (skill, ask_user_question, todo_write) are not inherited here — an allowlist would have to name
 * them and be refused. So this is a DENY of every inherited tool off the allowlist, read from the
 * core's own "known global tools" report, and extended whenever a late MCP server registers more.
 * @returns sync(reason) — idempotent; a no-op on an unscoped (host-level) context.
 */
function createScopeRestriction(ctx, diag) {
  const denied = new Set();
  let unscoped = false;
  let busy = false;
  return function sync(reason) {
    if (unscoped || busy) return;
    busy = true;
    try {
      let message = "";
      try {
        ctx.tools.restrict({ deny: [SCOPE_PROBE] });
      } catch (error) {
        message = String(error && error.message);
      }
      const known = restrictableFromMessage(message);
      if (!known) {
        // A host-level row (the web profile) has no scope; the agent/created path covers it.
        unscoped = /scoped context/.test(message);
        diag("scope-restrict-unavailable", { reason, unscoped, message: message.slice(0, 240) });
        return;
      }
      const deny = known.filter((toolName) => !ALLOWED_SET.has(toolName) && !denied.has(toolName));
      if (!deny.length) return;
      ctx.tools.restrict({ deny });
      for (const toolName of deny) denied.add(toolName);
      diag("scope-restricted", {
        reason,
        newly_denied: deny.length,
        denied_total: denied.size,
        inherited_allowed: known.filter((toolName) => ALLOWED_SET.has(toolName)),
      });
    } catch (error) {
      diag("scope-restrict-failed", { reason, message: String(error && error.message).slice(0, 240) });
    } finally {
      busy = false;
    }
  };
}

/** Every name the core reports as restrictable for a scope, or null when the report is absent. */
function restrictableFromMessage(message) {
  const match = /known global tools: (.*)$/.exec(String(message).split("\n").pop());
  if (!match) return null;
  return match[1]
    .split(", ")
    .map((toolName) => toolName.trim().replace(/^"|"$/g, ""))
    .filter((toolName) => toolName && toolName !== "(none)");
}

const BUILD_RESULT_TOOLS = new Set([
  "mcp__solidworks__submit_feature_graph",
  "mcp__solidworks__confirm_action",
]);
const HEAVY_EFFORTS = new Set(["high", "xhigh", "max"]);
const LIGHT_EFFORT = "medium";

/** The JSON envelope a SolidWorks tool returned, or null. */
function resultEnvelope(result) {
  for (const part of Array.isArray(result?.content) ? result.content : []) {
    if (part?.type !== "text" || typeof part.text !== "string") continue;
    try {
      const parsed = JSON.parse(part.text);
      if (parsed && typeof parsed === "object") return parsed;
    } catch {
      // not an envelope
    }
  }
  return null;
}

/**
 * Lower the reasoning effort for the mechanical steps after a successful build, and only those.
 *
 * Measured 2026-10-31 at gpt-6.1-sol/high: after the part existed, every verification, save and
 * finish step still cost 50–70 s of model time. The user chose (2026-10-31) to keep THEIR effort
 * for reading the drawing and writing the graph, drop to medium once the build in the current
 * turn has succeeded, and return to their effort the moment any SolidWorks result fails or a new
 * turn begins. The core logs a changed header and seeds the next step from it, so the chosen
 * effort is remembered here and restored explicitly. A model that does not offer medium is never
 * lowered: the core rejects an unsupported effort outright, so support is checked first.
 */
function createEffortPolicy(ctx, diag) {
  const agents = new WeakMap();
  const support = new Map();
  const entry = (agent) => {
    let state = agents.get(agent);
    if (!state) agents.set(agent, (state = { turn: undefined, builtTurn: undefined, failed: false,
      chosen: undefined, loweredLast: false }));
    return state;
  };
  const supportsLight = (config) => {
    const key = `${config.provider}\u0000${config.model}`;
    if (!support.has(key)) {
      support.set(key, Promise.resolve().then(() => ctx.get("llm").resolveCallConfig({
        provider: config.provider, model: config.model, reasoningEffort: LIGHT_EFFORT,
      })).then(() => true, () => false));
    }
    return support.get(key);
  };
  return {
    observe(exec, result) {
      if (!exec?.agent || typeof exec.name !== "string" || !exec.name.startsWith(CAD_TOOL_PREFIX)) return;
      const state = entry(exec.agent);
      const envelope = resultEnvelope(result);
      state.failed = result?.isError === true ||
        ["failed", "partial", "verification_failed"].includes(envelope?.status);
      if (!state.failed && BUILD_RESULT_TOOLS.has(exec.name) && envelope?.status === "completed" &&
          Array.isArray(envelope.completed_steps) && envelope.completed_steps.length > 0) {
        state.builtTurn = state.turn;
      }
    },
    async request(event, next) {
      const config = await next();
      if (!event?.agent || !config || typeof config !== "object") return config;
      const state = entry(event.agent);
      state.turn = event.turn;
      const seedIsOurs = state.loweredLast && config.reasoningEffort === LIGHT_EFFORT;
      if (!seedIsOurs && config.reasoningEffort !== undefined) state.chosen = config.reasoningEffort;
      const mechanical = state.builtTurn !== undefined && state.builtTurn === event.turn &&
        !state.failed && HEAVY_EFFORTS.has(state.chosen);
      if (mechanical && await supportsLight(config)) {
        state.loweredLast = true;
        diag("effort-lowered", { from: state.chosen, to: LIGHT_EFFORT, model: config.model });
        return { ...config, reasoningEffort: LIGHT_EFFORT };
      }
      state.loweredLast = false;
      if (seedIsOurs && state.chosen !== undefined && state.chosen !== LIGHT_EFFORT) {
        diag("effort-restored", { to: state.chosen, model: config.model });
        return { ...config, reasoningEffort: state.chosen };
      }
      return config;
    },
  };
}

const CATALOG_OPEN = "<available_skills>\n";
const CATALOG_CLOSE = "\n</available_skills>";

/**
 * The catalog prose with every entry but the allowed skills removed, or null when there is
 * nothing to remove (or no allowed skill would remain, which would leave the model no route).
 */
function filterCatalogText(text) {
  const start = text.indexOf(CATALOG_OPEN);
  const end = text.indexOf(CATALOG_CLOSE, start);
  if (start < 0 || end < 0) return null;
  const lines = text.slice(start + CATALOG_OPEN.length, end).split("\n");
  const kept = lines.filter((line) => ALLOWED_SKILL_SET.has(/^- `([^`]+)`:/.exec(line)?.[1]));
  if (!kept.length || kept.length === lines.length) return null;
  return {
    text: text.slice(0, start + CATALOG_OPEN.length) + kept.join("\n") + text.slice(end),
    kept: kept.length,
    dropped: lines.length - kept.length,
  };
}

/**
 * Narrow the skill catalog dsh-tool-skill appends in agent/pre-step to the allowed skills.
 *
 * Only the model-facing text changes. `source.entries` stays the complete list on purpose: the
 * catalog's change detection digests those entries against the live registry each step, so
 * rewriting them would make every later step republish a full replacement catalog.
 */
function filterSkillCatalog(decision, diag) {
  if (!decision || decision.kind === "reject" || !Array.isArray(decision.messages)) return decision;
  let changed = false;
  const messages = decision.messages.map((message) => {
    if (message?.source?.kind !== "skill-catalog" || !Array.isArray(message.content)) return message;
    let touched = false;
    const content = message.content.map((part) => {
      if (part?.type !== "text" || typeof part.text !== "string") return part;
      const filtered = filterCatalogText(part.text);
      if (!filtered) return part;
      touched = true;
      diag("skill-catalog-filtered", { kept: filtered.kept, dropped: filtered.dropped });
      return { ...part, text: filtered.text };
    });
    if (!touched) return message;
    changed = true;
    return { ...message, content };
  });
  return changed ? { ...decision, messages } : decision;
}

function apply(ctx) {
  const progress = createIntakeProgress(ctx.logger);
  const diag = createDiagnostics();
  const syncScope = createScopeRestriction(ctx, diag);
  const effort = createEffortPolicy(ctx, diag);
  syncScope("apply");
  ctx.on("agent/request", (event, next) => effort.request(event, next));
  // Prepended so this runs OUTSIDE dsh-tool-skill's listener and sees the catalog it appends.
  ctx.on("agent/pre-step", async (_event, next) => filterSkillCatalog(await next(), diag),
    { prepend: true });
  ctx.on("tools/change", () => { syncScope("tools/change"); });
  ctx.on("agent/pre-step", ({ agent, turn }, next) => {
    syncScope("agent/pre-step");
    progress.begin(agent, turn);
    return next();
  });
  ctx.tools.guard((exec) =>
    ALLOWED_SET.has(exec.name) ? progress.guard(exec) : denialReason(exec.name),
  );

  ctx.on("agent/created", ({ agent }) => {
    syncScope("agent/created");
    restrictAgent(agent, ctx.logger, diag);
  });

  ctx.on("agent/disposed", ({ agent }) => {
    pendingUserQuestions.delete(agent);
    progress.dispose(agent);
  });

  // tools/result also fires if cancellation or a later guard skips dispatch.
  ctx.on("tools/result", (exec, result) => {
    progress.observe(exec, result);
    effort.observe(exec, result);
    if (exec.name === QUESTION_TOOL && exec.agent) endUserQuestion(exec);
  });

  ctx.on("tools/execute", async (exec, next) => {
    if (exec.name !== QUESTION_TOOL || !exec.agent) return next();
    try {
      return await next();
    } finally {
      endUserQuestion(exec);
    }
  });

  ctx.on("tools/pre-execute", (exec, next) => {
    if (!ALLOWED_SET.has(exec.name)) {
      return { kind: "deny", reason: denialReason(exec.name) };
    }
    if (exec.name === SKILL_TOOL && !ALLOWED_SKILL_SET.has(String(exec.arguments?.name ?? ""))) {
      return {
        kind: "deny",
        reason: `Only the ${ALLOWED_SKILLS.join(", ")} skill is available in the SolidWorks profile.`,
      };
    }
    if (exec.name === QUESTION_TOOL && exec.agent) {
      beginUserQuestion(exec);
      return Promise.resolve().then(() => next()).then(
        (decision) => {
          if (decision?.kind !== "allow" && decision?.kind !== "ask") endUserQuestion(exec);
          return decision;
        },
        (error) => {
          endUserQuestion(exec);
          throw error;
        },
      );
    }
    if (
      exec.name.startsWith(CAD_TOOL_PREFIX) &&
      exec.agent &&
      (pendingUserQuestions.get(exec.agent)?.size ?? 0) > 0
    ) {
      return { kind: "deny", reason: QUESTION_WAIT_REASON };
    }
    if (exec.name === MODLENS_TOOL) {
      if (!isLocalImagePath(exec.arguments?.path)) {
        return { kind: "deny", reason: MODLENS_LOCAL_ONLY_REASON };
      }
    }
    if (IMAGE_READ_TOOLS.has(exec.name) && exec.name !== MODLENS_TOOL &&
        !isDrawingSource(exec.arguments?.image_path)) {
      return { kind: "deny", reason: REGION_LOCAL_ONLY_REASON };
    }
    if (exec.name !== CONFIRM_TOOL) {
      return next();
    }
    const summary = exec.arguments?.action_summary;
    const reason =
      typeof summary === "string" && summary.trim()
        ? summary
        : "Confirm the pending SolidWorks CAD action.";
    return Promise.resolve(next()).then((result) =>
      result?.kind === "allow" ? { kind: "ask", reason } : result,
    );
  });
}

export {
  ALLOWED_SKILLS,
  ALLOWED_TOOLS,
  SOLIDWORKS_TOOLS,
  SUPPORT_TOOLS,
  apply,
  filterSkillCatalog,
  inject,
  name,
  restrictableFromError,
};
