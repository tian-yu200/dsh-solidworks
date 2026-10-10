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
  "mcp__solidworks__finish_job",
];

const SUPPORT_TOOLS = [
  "ask_user_question",
  "todo_write",
  "skill",
  "modlens_read_image",
];

const ALLOWED_TOOLS = Object.freeze([
  ...SOLIDWORKS_TOOLS,
  ...SUPPORT_TOOLS,
]);
const ALLOWED_SET = new Set(ALLOWED_TOOLS);
const CONFIRM_TOOL = "mcp__solidworks__confirm_action";
const MODLENS_TOOL = "modlens_read_image";
const MODLENS_LOCAL_ONLY_REASON =
  "modlens_read_image accepts only a non-empty local image file path in the SolidWorks " +
  "profile; URL forms such as http://, https://, ftp:// and file:// are not allowed.";

function denialReason(toolName) {
  return (
    `Tool \`${toolName}\` is unavailable in the SolidWorks profile. ` +
    "Use the nine high-level SolidWorks MCP tools or the approved support tools."
  );
}

function apply(ctx) {
  ctx.tools.guard((exec) =>
    ALLOWED_SET.has(exec.name) ? undefined : denialReason(exec.name),
  );

  ctx.on("agent/created", ({ agent }) => {
    agent.ctx.tools.restrict({ allow: ALLOWED_TOOLS });
  });

  ctx.on("tools/pre-execute", (exec, next) => {
    if (!ALLOWED_SET.has(exec.name)) {
      return { kind: "deny", reason: denialReason(exec.name) };
    }
    if (exec.name === MODLENS_TOOL) {
      const imagePath = exec.arguments?.path;
      if (
        typeof imagePath !== "string" ||
        !imagePath.trim() ||
        /^[a-z][a-z0-9+.-]*:\/\//i.test(imagePath)
      ) {
        return { kind: "deny", reason: MODLENS_LOCAL_ONLY_REASON };
      }
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

export { ALLOWED_TOOLS, SOLIDWORKS_TOOLS, SUPPORT_TOOLS, apply, inject, name };
