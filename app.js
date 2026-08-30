const state = { status: null, session: null, taskId: null, prompt: "", story: "", seen: new Set() };
const el = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let payload;
  try { payload = await response.json(); } catch { payload = { error: `HTTP ${response.status}` }; }
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function brief() {
  return {
    format: el("format").value,
    product: el("product").value.trim(),
    audience: el("audience").value.trim(),
    objective: el("objective").value.trim(),
    acceptance_contract: el("acceptance").value.trim()
  };
}

function promptFor(value) {
  return `You are the director for a ${value.format.replaceAll("_", " ")}.

PRODUCT OR STORY
${value.product}

TARGET AUDIENCE
${value.audience}

OBJECTIVE
${value.objective}

ACCEPTANCE CONTRACT
${value.acceptance_contract}

Return one premise, three hooks, a final beat sheet, exactly six shots with duration/framing/action/dialogue/on-screen text/sound/continuity/exact render prompt, a CTA, and human approval risks.`;
}

function updatePipeline() {
  el("pipe-harness").textContent = el("harness").selectedOptions[0].textContent.split(" /")[0];
  el("pipe-control").textContent = el("control").selectedOptions[0].textContent.split(" /")[0];
  el("pipe-video").textContent = el("video-provider").selectedOptions[0].textContent;
  el("render").textContent = el("video-provider").value === "fake_video" ? "Render $0 proof" : "Plan paid render";
}

function setBusy(busy) {
  el("build").disabled = busy;
  el("build").textContent = busy ? "Building..." : "Build storyboard";
  if (busy) setOutputState("Working", "working");
}

function setOutputState(label, status = "neutral") {
  const target = el("output-state");
  target.textContent = label;
  target.className = `panel-step state-chip state-${status}`;
}

function showError(error) {
  el("empty").hidden = true;
  el("result").hidden = false;
  el("story").textContent = `Run blocked\n\n${error.message}`;
  el("story").className = "story status-bad";
  setOutputState("Blocked", "bad");
}

function addEvents(events) {
  for (const event of events) {
    const key = `${event.id}:${event.raw_event_hash || ""}`;
    if (state.seen.has(key)) continue;
    state.seen.add(key);
    const row = document.createElement("div");
    row.className = "event";
    const left = document.createElement("span");
    const right = document.createElement("span");
    left.textContent = event.type;
    right.textContent = event.native_type || event.id;
    row.append(left, right);
    el("events").append(row);
    if (event.type === "assistant.delta" && typeof event.text === "string") {
      state.story = event.native_type === "fixture.model.message" ? event.text : state.story + event.text;
    }
  }
  el("story").textContent = state.story || "Harness accepted the run. Waiting for output...";
}

async function pollEvents() {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    const payload = await api("/api/events", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ session: state.session })
    });
    addEvents(payload.events);
    if (payload.events.some((event) => ["turn.completed", "run.failed", "approval.required", "question.required"].includes(event.type))) return;
  }
}

async function buildStoryboard() {
  setBusy(true);
  state.seen.clear();
  state.story = "";
  el("events").replaceChildren();
  el("story").className = "story";
  const value = brief();
  state.prompt = promptFor(value);
  try {
    const created = await api("/api/session", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        harness: el("harness").value,
        control_provider: el("control").value,
        objective: value.objective,
        acceptance_contract: value.acceptance_contract
      })
    });
    state.session = created.session;
    state.taskId = created.work.work_id;
    const turn = await api("/api/turn", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ session: state.session, message: state.prompt, brief: value })
    });
    el("empty").hidden = true;
    el("result").hidden = false;
    addEvents(turn.events);
    if (!turn.events.some((event) => ["turn.completed", "run.failed", "approval.required", "question.required"].includes(event.type))) await pollEvents();
    el("render").disabled = false;
    setOutputState(turn.claim_status === "fixture" ? "Fixture proof" : "Storyboard ready", "good");
  } catch (error) {
    showError(error);
  } finally {
    setBusy(false);
  }
}

async function renderProof() {
  el("render").disabled = true;
  el("render").textContent = "Rendering...";
  try {
    const selected = el("video-provider").value;
    const prompt = state.story || state.prompt;
    const base = {
      task_id: state.taskId,
      session_id: state.session.session_id,
      prompt,
      aspect_ratio: el("aspect").value,
      duration_seconds: Number(el("duration").value)
    };
    let payload;
    if (selected === "fake_video") {
      payload = await api("/api/render/fake", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(base)
      });
    } else {
      const [provider, model] = selected.split(":", 2);
      if (model === "minimax_h3" && base.duration_seconds !== 4) base.duration_seconds = 4;
      const planned = await api("/api/render/plan", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ...base, provider, model })
      });
      const job = planned.job;
      const budget = job.estimated_credits == null ? "provider cost not measurable" : `${job.estimated_credits} credits`;
      const approved = window.confirm(`Approve this exact render?\n\nProvider: ${job.provider}\nModel: ${job.model}\nBudget: ${budget}\nPrompt hash: ${job.prompt_hash}\nJob hash: ${job.approval_hash}`);
      if (!approved) throw new Error("Render not approved. No generation was submitted.");
      payload = await api("/api/render/execute", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          ...base,
          provider,
          model,
          job_id: job.job_id,
          estimated_credits: job.estimated_credits,
          estimated_cost_usd: job.estimated_cost_usd,
          approval: {
            provider,
            job_approval_hash: job.approval_hash,
            maximum_credits: job.estimated_credits,
            maximum_cost_usd: job.estimated_cost_usd
          }
        })
      });
    }
    el("receipt-title").textContent = `${payload.result.provider || "Fixture"}: ${payload.result.status}`;
    el("receipt-meta").textContent = `${payload.receipt.output_hash || payload.result.artifact_id} | prompt stored: ${payload.receipt.raw_prompt_stored}`;
    const charge = payload.receipt.estimated_credits == null ? payload.receipt.estimated_cost_usd : payload.receipt.estimated_credits;
    el("cost").textContent = payload.receipt.estimated_credits == null ? `$${Number(charge || 0).toFixed(2)}` : `${charge} cr`;
    const outputUrl = payload.result.output_url || payload.result.content_url;
    if (outputUrl) {
      el("media-link").href = outputUrl;
      el("media-link").hidden = false;
    }
    setOutputState(payload.result.status === "submitted" ? "Render submitted" : "Render complete", "good");
  } catch (error) {
    showError(error);
  } finally {
    el("render").disabled = false;
    updatePipeline();
  }
}

async function initialize() {
  try {
    state.status = await api("/api/status");
    el("harness").value = state.status.default_harness;
    el("system-status").textContent = "Local gateway ready";
    const higgsfield = state.status.video_providers.find((provider) => provider.name === "higgsfield_cli");
    el("provider-note").textContent = `TrueForge: ${state.status.trueforge.configured ? "configured" : "fixture mode"}. Higgsfield: ${higgsfield?.available ? "connected" : "not ready"}. Paid generation always requires exact-job approval.`;
    if (!state.status.trueforge.configured) el("provider-note").className = "provider-note status-warn";
    updatePipeline();
  } catch (error) {
    el("system-status").textContent = "Gateway unavailable";
    el("live-dot").style.background = "var(--bad)";
    el("provider-note").textContent = "The local gateway could not be reached. Reconnect it before building a storyboard or planning a render.";
    el("provider-note").className = "provider-note status-bad";
    el("build").disabled = true;
    setOutputState("Gateway offline", "bad");
  }
}

el("build").addEventListener("click", buildStoryboard);
el("render").addEventListener("click", renderProof);
el("harness").addEventListener("change", updatePipeline);
el("control").addEventListener("change", updatePipeline);
el("video-provider").addEventListener("change", updatePipeline);
initialize();
