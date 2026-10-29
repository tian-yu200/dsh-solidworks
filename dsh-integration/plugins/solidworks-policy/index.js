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

function isLocalImagePath(value) {
  return (
    typeof value === "string" &&
    value.trim() !== "" &&
    !/^[a-z][a-z0-9+.-]*:\/\//i.test(value)
  );
}

function denialReason(toolName) {
  return (
    `Tool \`${toolName}\` is unavailable in the SolidWorks profile. ` +
    "Use the eleven high-level SolidWorks MCP tools or the approved support tools."
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

export { ALLOWED_TOOLS, SOLIDWORKS_TOOLS, SUPPORT_TOOLS, apply, inject, name };
