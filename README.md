# Touchdown AI Video Studio

Standalone local product surface for launch videos, TikToks, ads, microdramas,
and short films. It has no InferGuard dependency.

```text
HTML brief
  -> standalone Studio gateway
  -> fixture or TrueForge named agent
  -> storyboard events
  -> zero-cost render contract
  -> hash-only receipt
```

## Visual system

The Studio uses the Touchdown dashboard sibling system: Inter, JetBrains Mono
for evidence labels, the `#0066FF` accent, semantic run states, 6/10/14 px
radii, and one restrained panel shadow. Marketing and editorial square-card
rules do not apply to this operator surface.

## Run now

```bash
python3 server.py
```

Open `http://127.0.0.1:8788`. The default fixture generates a deterministic
six-shot structure and a zero-cost receipt. It does not call a model or produce
video pixels.

## Storage

The backend automatically stores each project, storyboard, render plan, and
receipt in `data/studio.sqlite3`. SQLite is built into Python; there is no
external database service or dependency. Generated video files remain with the
video provider and the project stores only their URL and receipt metadata.

```text
GET /api/projects
GET /api/projects/<project-id>
```

Set `TD_STUDIO_DB_PATH` to move the database. Raw model prompts and credentials
are never written to it.

## Connect Higgsfield

```bash
higgsfield auth login
higgsfield account status
```

The UI supports authenticated `seedance_2_0` and Higgsfield's hosted
`minimax_h3`. It first calls `higgsfield generate cost`, shows the exact credit
estimate and immutable job hash, and submits only after the operator confirms
that exact plan. Seedance 2.0 is the default general video model. No credential
is copied into the Studio.

## Connect TrueForge

Start TrueForge separately, create one named agent per planning provider, then:

```bash
export TRUEFORGE_BASE_URL=http://127.0.0.1:8790
export TD_STUDIO_TRUEFORGE_AGENTS='{"glm_zai_api":"touchdown-video-glm"}'
python3 server.py
```

The map is the switch: each UI planning-provider value resolves to one actual
TrueForge agent. The agent owns its model credentials and tools. The Studio owns
the brief, acceptance contract, provider choice, render gate, and receipt.

`codex_subscription` is not automatically available inside TrueForge. Map it
only after a harness adapter with a verified Codex OAuth path exists. Prime Agent
ACP and DeepSeek Harness ACP remain visible but disabled until those adapters are
implemented and tested.

## TrueForge and Qodo

They serve different paths:

```text
Video request -> Studio -> TrueForge named agent -> planning model -> storyboard
Pull request  -> GitHub CI -> Qodo review -> human merge decision
```

TrueForge is the runtime harness. It owns the planning-model session, tools,
and context. The Studio owns the brief, approval boundary, SQLite project, and
render receipt. The adapter is built and offline-tested; a live test requires a
TrueForge server on port 8790 with a saved agent named `touchdown-video-glm`.

Qodo is code review only. This repository includes `.pr_agent.toml`, but Qodo
does not run until its GitHub App has access to this private repository. After
installation, open a pull request; CI runs the seven local tests and Qodo reviews
the diff. Qodo never approves paid renders or changes runtime behavior.

## Connect an authorized MiniMax H3 endpoint

The official `MiniMaxAI/MiniMax-H3` repository exposes the SGLang contract:

```text
POST /v1/videos
GET  /v1/videos/{id}
GET  /v1/videos/{id}/content
```

Configure an already deployed endpoint with:

```bash
export MINIMAX_H3_BASE_URL=http://127.0.0.1:30010
export MINIMAX_H3_API_TOKEN='<only when the endpoint requires it>'
export TD_STUDIO_JURISDICTION=US
export MINIMAX_H3_LICENSE_AUTHORIZATION_ID='<written MiniMax authorization id>'
```

The open-weight license excludes the US, EU, UK, and South Korea unless MiniMax
provides separate written authorization. The adapter fails closed in those
regions without that id. Hugging Face authentication or paid GPU access does
not replace the license authorization. The source contract was pinned to model
revision `42ed227ee7df40d41602854ae760620d6eb651fe` on 2026-08-29.

Hugging Face Jobs is suitable for an authorized batch validation run, not a
durable low-latency product endpoint. Jobs require a paid account, a logged-in
token, multi-GPU hardware, a timeout, and explicit result persistence. No GPU
job has been launched by this app.

## Test

```bash
python3 -m unittest discover -s tests -v
```
