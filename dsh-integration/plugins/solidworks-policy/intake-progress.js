// Host-owned task identity: exact Agent object + agent/pre-step turn. No model reset API.
// Three consecutive failed/redundant reads justify stopping the SAME attempt, not a crop quota.
export const STALL_LIMIT = 3;
export const IMAGE_READ_TOOLS = new Set([
  'mcp__solidworks__read_drawing_region', 'mcp__solidworks__read_drawing_text', 'modlens_read_image',
]);
const REGION = 'mcp__solidworks__read_drawing_region';

function requestKey(exec) {
  const args = exec.arguments ?? {};
  return JSON.stringify([exec.name, args.image_path ?? args.path, args.region ?? '', args.regions ?? null, args.zoom ?? 2]);
}

function decoded(result) {
  const values = [];
  function walk(value, depth = 0) {
    if (!value || depth > 5) return;
    if (typeof value === 'string') {
      try { walk(JSON.parse(value), depth + 1); } catch {}
    } else if (Array.isArray(value)) value.forEach(item => walk(item, depth + 1));
    else if (typeof value === 'object') {
      if (value.status) values.push(value);
      if (value.text) walk(value.text, depth + 1);
      if (value.content) walk(value.content, depth + 1);
    }
  }
  walk(result?.value);
  walk(result?.content);
  return values;
}

function covered(box, zoom, earlier) {
  const area = (box[2] - box[0]) * (box[3] - box[1]);
  return area > 0 && earlier.some(old => old.zoom >= zoom &&
    Math.max(0, Math.min(box[2], old.box[2]) - Math.max(box[0], old.box[0])) *
    Math.max(0, Math.min(box[3], old.box[3]) - Math.max(box[1], old.box[1])) / area >= 0.7);
}

export function createIntakeProgress(logger) {
  const tasks = new WeakMap();
  function begin(agent, turn) {
    if (agent && tasks.get(agent)?.turn !== turn) tasks.set(agent, {
      turn, stalled: 0, requests: new Set(), blocked: new Set(), facts: new Set(), boxes: new Map(),
    });
  }
  function observe(exec, result) {
    const task = exec.agent && tasks.get(exec.agent);
    if (!task || !IMAGE_READ_TOOLS.has(exec.name)) return;
    const key = requestKey(exec);
    const source = exec.arguments?.image_path ?? exec.arguments?.path;
    const payloads = decoded(result);
    const failed = result?.isError || payloads.some(p => p.status === 'failed');
    let novel = false;
    if (!failed) {
      if (exec.name === REGION) {
        for (const payload of payloads) {
          const rows = payload.action === 'batch' ? payload.regions ?? [] : [payload];
          for (const row of rows) {
            if (row.status !== 'completed') continue;
            const box = row.region?.source_box;
            const zoom = row.zoom ?? 1;
            if (box) {
              const earlier = task.boxes.get(source) ?? [];
              if (!covered(box, zoom, earlier)) novel = true;
              earlier.push({ box, zoom, label: row.region?.label, requested: row.region?.requested });
              task.boxes.set(source, earlier);
            } else if (payload.action === 'overview' && !task.facts.has('overview:' + source)) {
              novel = true; task.facts.add('overview:' + source);
            }
          }
        }
      } else {
        // OCR result facts exclude elapsed time; image reads use source identity, not paths in logs.
        const fact = exec.name + ':' + source + ':' + JSON.stringify(payloads.map(p => p.texts ?? p.action));
        novel = !task.facts.has(fact);
        task.facts.add(fact);
      }
    }
    task.requests.add(key);
    if (novel) { task.stalled = 0; task.blocked.clear(); }
    else { task.stalled++; task.blocked.add(key); }
    if (task.stalled >= STALL_LIMIT) logger?.warn?.(
      `solidworks-policy: intake no-progress turn=${task.turn} consecutive=${task.stalled} tool=${exec.name}`);
  }
  function guard(exec) {
    const task = exec.agent && tasks.get(exec.agent);
    if (!task || !IMAGE_READ_TOOLS.has(exec.name) || task.stalled < STALL_LIMIT) return;
    const source = exec.arguments?.image_path ?? exec.arguments?.path;
    const earlier = task.boxes.get(source) ?? [];
    const redundantRegion = region => {
      if (!region) return task.facts.has('overview:' + source);
      const known = earlier.find(old => old.label?.toUpperCase() === String(region).trim().toUpperCase() || old.requested === region);
      const coordinates = String(region).split(',').map(Number);
      const box = known?.box ?? (coordinates.length === 4 && coordinates.every(Number.isFinite) ? coordinates : null);
      return box && covered(box, exec.arguments?.zoom ?? 2, earlier);
    };
    const redundant = exec.name === REGION && (Array.isArray(exec.arguments?.regions) ? exec.arguments.regions : [exec.arguments?.region ?? '']).every(redundantRegion);
    if (!task.blocked.has(requestKey(exec)) && !redundant) return; // New evidence can recover.
    return `INTAKE_NO_PROGRESS: ${task.stalled} consecutive failed or redundant image reads in this task. ` +
      'Do not repeat this attempt. Report the evidence/tool failure once; do not label it a missing dimension. ' +
      'Use existing evidence for the compact plan, or change the source/region/zoom only when that can add evidence.';
  }
  return { begin, observe, guard, dispose: agent => tasks.delete(agent) };
}
