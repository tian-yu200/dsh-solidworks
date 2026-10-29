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
  "mcp__solidworks__read_drawing_region",
];

const SUPPORT_TOOLS = [
  "ask_user_question",
  "todo_write",
  "skill",
  "modlens_read_image",
  // The skill's references are served as resources by the solidworks MCP server, and this preset
  // has no file tool — without these two the "read the reference" instruction in SKILL.md would
  // itself be refused. Read-only: they list and read, never write.
  "read_mcp_resource",
  "list_mcp_resources",
];

const ALLOWED_TOOLS = Object.freeze([
  ...SOLIDWORKS_TOOLS,
  ...SUPPORT_TOOLS,
]);
const ALLOWED_SET = new Set(ALLOWED_TOOLS);
const CONFIRM_TOOL = "mcp__solidworks__confirm_action";
const MODLENS_TOOL = "modlens_read_image";
const REGION_TOOL = "mcp__solidworks__read_drawing_region";
const MODLENS_LOCAL_ONLY_REASON =
  "modlens_read_image accepts only a non-empty local image file path in the SolidWorks " +
  "profile; URL forms such as http://, https://, ftp:// and file:// are not allowed.";
const REGION_LOCAL_ONLY_REASON =
  "read_drawing_region accepts only a non-empty local image file path; URL forms such as " +
  "http://, https://, ftp:// and file:// are not allowed. Save the attachment locally first.";

function denialReason(toolName) {
  return (
    `Tool \`${toolName}\` is unavailable in the SolidWorks profile. ` +
    "Use the eleven high-level SolidWorks MCP tools or the approved support tools."
  );
}

function isLocalImagePath(value) {
  return (
    typeof value === "string" &&
    value.trim() !== "" &&
    !/^[a-z][a-z0-9+.-]*:\/\//i.test(value)
  );
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
  const match = /known global tools: (.*)$/.exec(String(message).split("\n").pop());
  if (!match) return null;
  const known = match[1]
    .split(", ")
    .map((name) => name.trim().replace(/^"|"$/g, ""))
    .filter(Boolean);
  if (!known.length) return null;
  return ALLOWED_TOOLS.filter((toolName) => known.includes(toolName));
}

function restrictAgent(agent, logger) {
  const tools = agent.ctx.tools;
  try {
    tools.restrict({ allow: ALLOWED_TOOLS });
    return;
  } catch (error) {
    const allow = restrictableFromError(error && error.message);
    if (!allow) {
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
    tools.restrict({ allow });
  }
}

function apply(ctx) {
  ctx.tools.guard((exec) =>
    ALLOWED_SET.has(exec.name) ? undefined : denialReason(exec.name),
  );

  ctx.on("agent/created", ({ agent }) => {
    restrictAgent(agent, ctx.logger);
  });

  ctx.on("tools/pre-execute", (exec, next) => {
    if (!ALLOWED_SET.has(exec.name)) {
      return { kind: "deny", reason: denialReason(exec.name) };
    }
    if (exec.name === MODLENS_TOOL) {
      if (!isLocalImagePath(exec.arguments?.path)) {
        return { kind: "deny", reason: MODLENS_LOCAL_ONLY_REASON };
      }
    }
    if (exec.name === REGION_TOOL && !isLocalImagePath(exec.arguments?.image_path)) {
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
  ALLOWED_TOOLS,
  SOLIDWORKS_TOOLS,
  SUPPORT_TOOLS,
  apply,
  inject,
  name,
  restrictableFromError,
};
