from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Protocol, cast

from renderers import HiggsfieldRenderer, MiniMaxH3EndpointRenderer, PaidApproval, RenderJob
from storage import SQLiteProjectStore


APP_ROOT = Path(__file__).resolve().parent
STATIC_FILES = {
    "/": (APP_ROOT / "index.html", "text/html; charset=utf-8"),
    "/index.html": (APP_ROOT / "index.html", "text/html; charset=utf-8"),
    "/styles.css": (APP_ROOT / "styles.css", "text/css; charset=utf-8"),
    "/app.js": (APP_ROOT / "app.js", "text/javascript; charset=utf-8"),
}
MAX_JSON_BYTES = 128 * 1024
CONTROL_PROVIDERS = {
    "glm_zai_api": "GLM-5.3 Flash / Z.ai",
    "codex_subscription": "Codex subscription via compatible harness",
    "glm_local_sglang": "Self-hosted GLM / SGLang",
    "glm_local_vllm": "Self-hosted GLM / vLLM",
}
FORMATS = {"startup_launch", "tiktok", "performance_ad", "microdrama", "short_film"}
ASPECT_RATIOS = {"9:16", "16:9", "1:1"}


def hash_value(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def required_text(payload: Mapping[str, Any], key: str, maximum: int = 24_000) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise StudioError(f"{key} is required")
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise StudioError(f"{key} exceeds {maximum} characters")
    return cleaned


class StudioError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class JSONTransport(Protocol):
    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]: ...


@dataclass
class UrlTransport:
    base_url: str
    bearer_token: str | None = None
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("TrueForge base URL must be absolute HTTP(S)")
        if self.bearer_token and parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("bearer tokens require HTTPS except on loopback")

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = urllib.parse.urljoin(self.base_url.rstrip("/") + "/", path.lstrip("/"))
        headers = {"accept": "application/json"}
        data = None
        if body is not None:
            headers["content-type"] = "application/json"
            data = json.dumps(body, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if self.bearer_token:
            headers["authorization"] = f"Bearer {self.bearer_token}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            value = json.loads(response.read().decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("TrueForge returned non-object JSON")
        return value


@dataclass(frozen=True)
class StudioSession:
    session_id: str
    native_session_id: str
    harness: str
    control_provider: str
    work_id: str
    agent_name: str | None = None


class TrueForgeHarness:
    name = "trueforge_http"

    def __init__(self, transport: JSONTransport, agent_by_control: Mapping[str, str]) -> None:
        self.transport = transport
        self.agent_by_control = {key: value for key, value in agent_by_control.items() if value.strip()}

    def create_session(self, control_provider: str, work_id: str) -> StudioSession:
        agent_name = self.agent_by_control.get(control_provider)
        if not agent_name:
            raise StudioError(
                f"TrueForge has no named agent mapped for {control_provider}; configure TD_STUDIO_TRUEFORGE_AGENTS",
                409,
            )
        response = self.transport.request("POST", "/api/v1/sessions", {"agent": {"name": agent_name}})
        native_id = self._data_id(response, "session")
        return StudioSession(
            session_id=hash_value({"harness": self.name, "native_session_id": native_id}),
            native_session_id=native_id,
            harness=self.name,
            control_provider=control_provider,
            work_id=work_id,
            agent_name=agent_name,
        )

    def send_turn(self, session: StudioSession, message: str) -> str:
        response = self.transport.request(
            "POST",
            f"/api/v1/sessions/{urllib.parse.quote(session.native_session_id, safe='')}/turns",
            {
                "input": [{"type": "user.message", "content": message}],
                "previous_turn_id": "auto",
                "stream": False,
            },
        )
        return self._data_id(response, "turn")

    def events(self, session: StudioSession) -> list[dict[str, Any]]:
        response = self.transport.request(
            "GET", f"/api/v1/sessions/{urllib.parse.quote(session.native_session_id, safe='')}/events"
        )
        rows = response.get("data", [])
        if not isinstance(rows, list):
            raise ValueError("TrueForge events response data must be a list")
        events: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            native = row.get("event") if isinstance(row.get("event"), dict) else row
            native_type = str(native.get("type") or row.get("type") or "harness.event")
            content = native.get("content")
            event = {
                "id": str(row.get("id") or row.get("sequence_number") or index),
                "type": canonical_event_type(native_type),
                "native_type": native_type,
                "raw_event_hash": hash_value(row),
            }
            if isinstance(content, str) and native_type in {"model.message", "model.message.delta"}:
                event["text"] = content
            events.append(event)
        return events

    @staticmethod
    def _data_id(response: Mapping[str, Any], kind: str) -> str:
        data = response.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"].strip():
            raise ValueError(f"TrueForge {kind} response missing data.id")
        return data["id"]


def canonical_event_type(native_type: str) -> str:
    lowered = native_type.lower()
    if "approval" in lowered:
        return "approval.required"
    if "response_required" in lowered or "question" in lowered:
        return "question.required"
    if "message" in lowered or "delta" in lowered:
        return "assistant.delta"
    if "cancel" in lowered:
        return "run.cancelled"
    if "complete" in lowered or "done" in lowered or "end" in lowered:
        return "turn.completed"
    if "fail" in lowered or "error" in lowered:
        return "run.failed"
    return "harness.event"


class StudioService:
    def __init__(
        self,
        environment: Mapping[str, str] | None = None,
        transport: JSONTransport | None = None,
        store: SQLiteProjectStore | None = None,
    ) -> None:
        self.environment = dict(environment if environment is not None else os.environ)
        database_path = self.environment.get("TD_STUDIO_DB_PATH", str(APP_ROOT / "data" / "studio.sqlite3"))
        self.store = store or SQLiteProjectStore(database_path)
        self.trueforge_agents = self._agent_map()
        self.trueforge: TrueForgeHarness | None = None
        self.higgsfield = HiggsfieldRenderer()
        self.minimax_h3 = MiniMaxH3EndpointRenderer(self.environment)
        base_url = self.environment.get("TRUEFORGE_BASE_URL", "").strip()
        if base_url and self.trueforge_agents:
            self.trueforge = TrueForgeHarness(
                transport
                or UrlTransport(base_url, self.environment.get("TRUEFORGE_BEARER_TOKEN") or None),
                self.trueforge_agents,
            )

    def _agent_map(self) -> dict[str, str]:
        raw = self.environment.get("TD_STUDIO_TRUEFORGE_AGENTS", "").strip()
        if raw:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("TD_STUDIO_TRUEFORGE_AGENTS must be a JSON object") from exc
            if not isinstance(value, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
                raise ValueError("TD_STUDIO_TRUEFORGE_AGENTS must map provider names to TrueForge agent names")
            return value
        legacy = self.environment.get("TRUEFORGE_AGENT_NAME", "").strip()
        return {"glm_zai_api": legacy} if legacy else {}

    def status(self) -> dict[str, Any]:
        higgsfield_status = self.higgsfield.status()
        minimax_status = self.minimax_h3.status()
        return {
            "ok": True,
            "product": "Touchdown AI Video Studio",
            "inferguard_dependency": False,
            "default_harness": "trueforge_http" if self.trueforge else "fixture_harness",
            "trueforge": {
                "configured": self.trueforge is not None,
                "base_url": self.environment.get("TRUEFORGE_BASE_URL", "http://127.0.0.1:8790"),
                "agent_mappings": sorted(self.trueforge_agents),
            },
            "harnesses": [
                {"name": "fixture_harness", "available": True, "claim_status": "fixture"},
                {"name": "trueforge_http", "available": self.trueforge is not None, "claim_status": "built"},
                {"name": "prime_agent_acp", "available": False, "claim_status": "proposed"},
                {"name": "deepseek_harness_acp", "available": False, "claim_status": "proposed"},
            ],
            "control_providers": [
                {"name": name, "label": label, "live_with_trueforge": name in self.trueforge_agents}
                for name, label in CONTROL_PROVIDERS.items()
            ],
            "video_providers": [
                {"name": "fake_video", "available": True, "cost_usd": 0.0, "claim_status": "fixture"},
                {
                    "name": "higgsfield_cli",
                    "available": higgsfield_status["installed"] and higgsfield_status["authenticated"],
                    "claim_status": "approval_required",
                    **higgsfield_status,
                },
                {
                    "name": "minimax_h3_endpoint",
                    "available": minimax_status["configured"] and minimax_status["license_ready"],
                    "claim_status": "approval_required" if minimax_status["license_ready"] else "license_blocked",
                    **minimax_status,
                },
            ],
            "storage": {
                "type": "sqlite",
                "persistent": self.store.persistent,
                "database": Path(self.store.database_path).name,
            },
            "privacy": {
                "project_brief_stored": True,
                "storyboard_stored": True,
                "raw_model_prompt_stored": False,
                "credentials_stored": False,
                "training_allowed": False,
            },
        }

    def create_session(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        harness = required_text(payload, "harness", 64)
        control = required_text(payload, "control_provider", 64)
        objective = required_text(payload, "objective")
        acceptance = required_text(payload, "acceptance_contract", 4_000)
        if control not in CONTROL_PROVIDERS:
            raise StudioError(f"unknown control provider: {control}")
        work_id = f"work_{uuid.uuid4().hex[:12]}"
        if harness == "fixture_harness":
            native_id = f"fixture_{uuid.uuid4().hex[:12]}"
            session = StudioSession(hash_value(native_id), native_id, harness, control, work_id)
        elif harness == "trueforge_http":
            if not self.trueforge:
                raise StudioError("TrueForge is not configured; use the fixture or set the TrueForge environment", 409)
            try:
                session = self.trueforge.create_session(control, work_id)
            except StudioError:
                raise
            except Exception as exc:
                raise StudioError(f"TrueForge session failed: {exc}", 502) from exc
        else:
            raise StudioError(f"harness is not executable yet: {harness}", 409)
        result = {
            "session": asdict(session),
            "work": {
                "work_id": work_id,
                "objective_hash": hash_value(objective),
                "acceptance_contract_hash": hash_value(acceptance),
                "control_provider": control,
            },
        }
        self.store.create(
            work_id,
            {
                "status": "created",
                "session": result["session"],
                "work": result["work"],
                "brief": {
                    "objective": objective,
                    "acceptance_contract": acceptance,
                },
            },
        )
        return result

    def turn(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        session = self._session(payload)
        message = required_text(payload, "message")
        if session.harness == "fixture_harness":
            brief = payload.get("brief") if isinstance(payload.get("brief"), dict) else {}
            text = fixture_storyboard(brief)
            turn_id = f"turn_{uuid.uuid4().hex[:12]}"
            result = {
                "turn_id": turn_id,
                "events": [
                    {"id": f"{turn_id}:start", "type": "turn.started", "native_type": "fixture.turn.started"},
                    {
                        "id": f"{turn_id}:message",
                        "type": "assistant.delta",
                        "native_type": "fixture.model.message",
                        "text": text,
                    },
                    {"id": f"{turn_id}:done", "type": "turn.completed", "native_type": "fixture.turn.done"},
                ],
                "claim_status": "fixture",
            }
            self._save_storyboard(session.work_id, brief, text, result["claim_status"])
            return result
        if session.harness != "trueforge_http" or not self.trueforge:
            raise StudioError("session harness is unavailable", 409)
        try:
            turn_id = self.trueforge.send_turn(session, message)
            events = self.trueforge.events(session)
        except Exception as exc:
            raise StudioError(f"TrueForge turn failed: {exc}", 502) from exc
        result = {"turn_id": turn_id, "events": events, "claim_status": "live_provider"}
        storyboard = "".join(
            str(event.get("text", ""))
            for event in events
            if event.get("type") == "assistant.delta" and isinstance(event.get("text"), str)
        )
        self._save_storyboard(session.work_id, {}, storyboard, result["claim_status"])
        return result

    def events(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        session = self._session(payload)
        if session.harness != "trueforge_http" or not self.trueforge:
            return {"events": [], "claim_status": "fixture"}
        try:
            return {"events": self.trueforge.events(session), "claim_status": "live_provider"}
        except Exception as exc:
            raise StudioError(f"TrueForge event read failed: {exc}", 502) from exc

    def fake_render(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        task_id = required_text(payload, "task_id", 128)
        session_id = required_text(payload, "session_id", 256)
        prompt = required_text(payload, "prompt")
        aspect_ratio = str(payload.get("aspect_ratio") or "9:16")
        if aspect_ratio not in ASPECT_RATIOS:
            raise StudioError("aspect_ratio must be 9:16, 16:9, or 1:1")
        duration = payload.get("duration_seconds", 6)
        if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 60:
            raise StudioError("duration_seconds must be an integer from 1 to 60")
        job_id = f"video_{uuid.uuid4().hex[:12]}"
        prompt_hash = hash_value(prompt)
        approval_hash = hash_value(
            {"job_id": job_id, "task_id": task_id, "prompt_hash": prompt_hash, "aspect_ratio": aspect_ratio, "duration": duration}
        )
        artifact_id = hash_value({"approval_hash": approval_hash, "fixture": True})
        result = {
            "result": {
                "job_id": job_id,
                "artifact_id": artifact_id,
                "status": "fixture",
                "produced_video_pixels": False,
                "estimated_cost_usd": 0.0,
            },
            "job": {
                "job_id": job_id,
                "task_id": task_id,
                "provider": "fake_video",
                "model": "fixture-video-v1",
                "prompt_hash": prompt_hash,
                "approval_hash": approval_hash,
                "aspect_ratio": aspect_ratio,
                "duration_seconds": duration,
            },
            "receipt": {
                "provider_call_id": f"video_{job_id}",
                "connector_session_id": session_id,
                "provider": "fake_video",
                "status": "success",
                "provenance": "replay",
                "estimated_cost_usd": 0.0,
                "prompt_hash": prompt_hash,
                "output_hash": artifact_id,
                "raw_prompt_stored": False,
                "raw_output_stored": False,
            },
        }
        self._save_render(task_id, result)
        return result

    def plan_render(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        job = self._render_job(payload)
        try:
            if job.provider == "higgsfield_cli":
                planned = self.higgsfield.plan(job)
            elif job.provider == "minimax_h3_endpoint":
                planned = self.minimax_h3.plan(job)
            else:
                raise StudioError(f"provider does not support paid planning: {job.provider}")
        except PermissionError as exc:
            raise StudioError(str(exc), 403) from exc
        except Exception as exc:
            raise StudioError(f"render plan failed: {exc}", 502) from exc
        result = {"job": planned.safe_json(), "requires_paid_approval": True}
        self._update_project(job.task_id, {"status": "render_planned", "latest_render_plan": result})
        return result

    def execute_render(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        job = self._render_job(payload)
        approval_raw = payload.get("approval")
        if not isinstance(approval_raw, dict):
            raise StudioError("approval is required", 403)
        try:
            approval = PaidApproval(
                provider=required_text(approval_raw, "provider", 64),
                job_approval_hash=required_text(approval_raw, "job_approval_hash", 256),
                maximum_credits=self._optional_positive_float(approval_raw.get("maximum_credits")),
                maximum_cost_usd=self._optional_positive_float(approval_raw.get("maximum_cost_usd")),
            )
            if job.provider == "higgsfield_cli":
                result = self.higgsfield.execute(job, approval)
            elif job.provider == "minimax_h3_endpoint":
                result = self.minimax_h3.execute(job, approval)
            else:
                raise StudioError(f"provider is not executable: {job.provider}")
            self._save_render(job.task_id, result)
            return result
        except PermissionError as exc:
            raise StudioError(str(exc), 403) from exc
        except StudioError:
            raise
        except Exception as exc:
            raise StudioError(f"render failed: {exc}", 502) from exc

    def _render_job(self, payload: Mapping[str, Any]) -> RenderJob:
        provider = required_text(payload, "provider", 64)
        model = required_text(payload, "model", 128)
        if provider == "higgsfield_cli" and model not in {"seedance_2_0", "minimax_h3"}:
            raise StudioError("Higgsfield model must be seedance_2_0 or minimax_h3")
        if provider == "minimax_h3_endpoint" and model != "MiniMaxAI/MiniMax-H3":
            raise StudioError("MiniMax H3 endpoint model must be MiniMaxAI/MiniMax-H3")
        aspect_ratio = str(payload.get("aspect_ratio") or "9:16")
        if aspect_ratio not in ASPECT_RATIOS:
            raise StudioError("aspect_ratio must be 9:16, 16:9, or 1:1")
        duration = payload.get("duration_seconds", 5)
        if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 15:
            raise StudioError("duration_seconds must be an integer from 1 to 15")
        estimated_credits = self._optional_positive_float(payload.get("estimated_credits"))
        estimated_cost_usd = self._optional_positive_float(payload.get("estimated_cost_usd"))
        return RenderJob(
            job_id=str(payload.get("job_id") or f"video_{uuid.uuid4().hex[:12]}"),
            task_id=required_text(payload, "task_id", 128),
            session_id=required_text(payload, "session_id", 256),
            provider=provider,
            model=model,
            prompt=required_text(payload, "prompt"),
            aspect_ratio=aspect_ratio,
            duration_seconds=duration,
            estimated_credits=estimated_credits,
            estimated_cost_usd=estimated_cost_usd,
        )

    def projects(self) -> dict[str, Any]:
        projects = self.store.list()
        return {
            "projects": [
                {
                    "project_id": project["project_id"],
                    "created_at": project["created_at"],
                    "updated_at": project["updated_at"],
                    "status": project.get("status", "unknown"),
                    "control_provider": project.get("work", {}).get("control_provider"),
                }
                for project in projects
            ]
        }

    def project(self, project_id: str) -> dict[str, Any]:
        project = self.store.get(project_id)
        if project is None:
            raise StudioError("project not found", 404)
        return {"project": project}

    def _save_storyboard(
        self,
        project_id: str,
        brief: Mapping[str, Any],
        storyboard: str,
        claim_status: str,
    ) -> None:
        patch: dict[str, Any] = {
            "status": "storyboard_ready",
            "storyboard": storyboard,
            "storyboard_claim_status": claim_status,
        }
        if brief:
            patch["brief"] = dict(brief)
        self._update_project(project_id, patch)

    def _save_render(self, project_id: str, result: Mapping[str, Any]) -> None:
        render_status = result.get("result", {}).get("status", "rendered")
        self._update_project(project_id, {"status": render_status, "latest_render": dict(result)})

    def _update_project(self, project_id: str, patch: Mapping[str, Any]) -> None:
        try:
            self.store.update(project_id, patch)
        except KeyError as exc:
            raise StudioError("project not found", 404) from exc

    @staticmethod
    def _optional_positive_float(value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise StudioError("budget values must be positive numbers")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise StudioError("budget values must be positive numbers") from exc
        if number <= 0:
            raise StudioError("budget values must be positive numbers")
        return number

    @staticmethod
    def _session(payload: Mapping[str, Any]) -> StudioSession:
        raw = payload.get("session")
        if not isinstance(raw, dict):
            raise StudioError("session is required")
        return StudioSession(
            session_id=required_text(raw, "session_id", 256),
            native_session_id=required_text(raw, "native_session_id", 256),
            harness=required_text(raw, "harness", 64),
            control_provider=required_text(raw, "control_provider", 64),
            work_id=required_text(raw, "work_id", 128),
            agent_name=raw.get("agent_name") if isinstance(raw.get("agent_name"), str) else None,
        )


def fixture_storyboard(brief: Mapping[str, Any]) -> str:
    format_name = str(brief.get("format") or "video").replace("_", " ")
    product = str(brief.get("product") or "the product")
    audience = str(brief.get("audience") or "the audience")
    objective = str(brief.get("objective") or "show the transformation")
    return f"""FIXTURE STORYBOARD - STRUCTURE PROOF, NOT MODEL OUTPUT

PREMISE
{product} helps {audience} move from a painful before-state to a clear after-state.

HOOK
What if the work that takes all day started with one sentence?

BEAT SHEET
Problem -> tension -> reveal -> proof -> transformation -> call to action.

01 / 0-2s / EXTREME CLOSE-UP
Action: Open on the most painful moment before {product}.
Text: This should not take all day.
Render prompt: High-contrast {format_name}, extreme close-up, immediate motion, vertical-safe composition.

02 / 2-4s / FAST MONTAGE
Action: Show three failed attempts escalating the cost.
Text: Too many tools. No finished story.
Render prompt: Three fast editorial inserts, rising tension, matched lighting and wardrobe continuity.

03 / 4-6s / HERO REVEAL
Action: Reveal {product} from one plain-English prompt.
Text: One brief. One production loop.
Render prompt: Clean hero reveal, controlled camera push, product UI reflected in the creator's face.

04 / 6-9s / PROCESS PROOF
Action: Show story, shots, and render plan appearing in sequence.
Text: Story -> shots -> video.
Render prompt: Rhythmic interface inserts, legible typography, each artifact appearing on beat.

05 / 9-12s / AFTER STATE
Action: Show the finished piece playing to {audience}.
Text: Ready to publish.
Render prompt: Confident creator, finished video on phone, continuous character identity and environment.

06 / 12-15s / CTA
Action: End on the promised outcome: {objective}.
Text: Make the first cut now.
Render prompt: Minimal end card, strong brand mark, one CTA, two-second hold for readability.

HUMAN CHECK
Approve brand claims, likeness rights, continuity, final copy, and any paid render before release."""


class StudioRequestHandler(BaseHTTPRequestHandler):
    server_version = "touchdown-ai-video-studio/0.1"

    @property
    def service(self) -> StudioService:
        return cast("StudioHTTPServer", self.server).service

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path in STATIC_FILES:
            file_path, content_type = STATIC_FILES[path]
            self._bytes(200, file_path.read_bytes(), content_type)
            return
        if path == "/healthz":
            self._json(200, {"ok": True})
            return
        if path == "/api/status":
            self._json(200, self.service.status())
            return
        if path == "/api/projects":
            self._json(200, self.service.projects())
            return
        if path.startswith("/api/projects/"):
            project_id = urllib.parse.unquote(path.removeprefix("/api/projects/"))
            try:
                self._json(200, self.service.project(project_id))
            except StudioError as exc:
                self._json(exc.status, {"error": str(exc)})
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        routes = {
            "/api/session": self.service.create_session,
            "/api/turn": self.service.turn,
            "/api/events": self.service.events,
            "/api/render/fake": self.service.fake_render,
            "/api/render/plan": self.service.plan_render,
            "/api/render/execute": self.service.execute_render,
        }
        action = routes.get(path)
        if action is None:
            self._json(404, {"error": "not_found"})
            return
        try:
            self._json(200, action(self._read_json()))
        except StudioError as exc:
            self._json(exc.status, {"error": str(exc)})
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": "invalid_json"})

    def _read_json(self) -> dict[str, Any]:
        if self.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise StudioError("content-type must be application/json", 415)
        raw_length = self.headers.get("content-length")
        if raw_length is None:
            raise StudioError("content-length is required", 411)
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise StudioError("invalid content-length") from exc
        if not 0 < length <= MAX_JSON_BYTES:
            raise StudioError("request body is empty or too large", 413)
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise StudioError("JSON body must be an object")
        return payload

    def _json(self, status: int, payload: Mapping[str, Any]) -> None:
        self._bytes(status, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8")

    def _bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("referrer-policy", "no-referrer")
        self.send_header(
            "content-security-policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


class StudioHTTPServer(ThreadingHTTPServer):
    service: StudioService

    def server_close(self) -> None:
        service = getattr(self, "service", None)
        if service is not None:
            service.store.close()
        super().server_close()


def create_server(host: str = "127.0.0.1", port: int = 8788, service: StudioService | None = None) -> StudioHTTPServer:
    server = StudioHTTPServer((host, port), StudioRequestHandler)
    server.service = service or StudioService()
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the standalone Touchdown AI Video Studio.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()
    server = create_server(args.host, args.port)
    print(f"Touchdown AI Video Studio listening on http://{server.server_address[0]}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
