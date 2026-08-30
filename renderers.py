from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping, Sequence


RunCommand = Callable[..., subprocess.CompletedProcess[str]]
EXCLUDED_H3_TERRITORIES = {"US", "EU", "UK", "KR", "SOUTH_KOREA"}


def hash_value(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RenderJob:
    job_id: str
    task_id: str
    session_id: str
    provider: str
    model: str
    prompt: str = field(repr=False)
    aspect_ratio: str = "9:16"
    duration_seconds: int = 6
    short_edge: int = 768
    estimated_credits: float | None = None
    estimated_cost_usd: float | None = None

    @property
    def prompt_hash(self) -> str:
        return hash_value(self.prompt)

    @property
    def approval_hash(self) -> str:
        return hash_value(
            {
                "job_id": self.job_id,
                "task_id": self.task_id,
                "provider": self.provider,
                "model": self.model,
                "prompt_hash": self.prompt_hash,
                "aspect_ratio": self.aspect_ratio,
                "duration_seconds": self.duration_seconds,
                "short_edge": self.short_edge,
                "estimated_credits": self.estimated_credits,
                "estimated_cost_usd": self.estimated_cost_usd,
            }
        )

    def safe_json(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("prompt")
        value["prompt_hash"] = self.prompt_hash
        value["approval_hash"] = self.approval_hash
        return value


@dataclass(frozen=True)
class PaidApproval:
    provider: str
    job_approval_hash: str
    maximum_credits: float | None = None
    maximum_cost_usd: float | None = None

    def assert_authorizes(self, job: RenderJob) -> None:
        if self.provider != job.provider or self.job_approval_hash != job.approval_hash:
            raise PermissionError("approval does not match the exact render job")
        if job.estimated_credits is not None:
            if self.maximum_credits is None or self.maximum_credits < job.estimated_credits:
                raise PermissionError("estimated credits exceed the approved maximum")
        if job.estimated_cost_usd is not None:
            if self.maximum_cost_usd is None or self.maximum_cost_usd < job.estimated_cost_usd:
                raise PermissionError("estimated cost exceeds the approved maximum")


class HiggsfieldRenderer:
    provider_name = "higgsfield_cli"

    def __init__(self, executable: str = "higgsfield", run: RunCommand = subprocess.run) -> None:
        self.executable = executable
        self.run = run

    def status(self) -> dict[str, Any]:
        installed = shutil.which(self.executable) is not None
        if not installed:
            return {"installed": False, "authenticated": False, "reason": "higgsfield CLI not installed"}
        completed = self.run(
            [self.executable, "account", "status"], capture_output=True, text=True, timeout=5, check=False
        )
        return {
            "installed": True,
            "authenticated": completed.returncode == 0,
            "reason": None if completed.returncode == 0 else (completed.stderr.strip() or completed.stdout.strip()),
        }

    def plan(self, job: RenderJob) -> RenderJob:
        command = self._command("cost", job, wait=False)
        completed = self.run(command, capture_output=True, text=True, timeout=30, check=False)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "Higgsfield cost check failed")
        credits = self._credits(completed.stdout)
        return RenderJob(**{**asdict(job), "estimated_credits": credits})

    def execute(self, job: RenderJob, approval: PaidApproval) -> dict[str, Any]:
        approval.assert_authorizes(job)
        started = time.monotonic()
        completed = self.run(
            self._command("create", job, wait=True),
            capture_output=True,
            text=True,
            timeout=30 * 60,
            check=False,
        )
        latency_ms = (time.monotonic() - started) * 1000
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "Higgsfield render failed")
        value = json.loads(completed.stdout)
        output = value[0] if isinstance(value, list) and value else value
        if not isinstance(output, dict):
            raise ValueError("Higgsfield returned invalid JSON")
        output_id = str(output.get("id") or output.get("job_id") or "")
        output_url = self._find_url(output)
        return {
            "result": {"status": "success", "provider": self.provider_name, "output_url": output_url},
            "receipt": self._receipt(job, output_id or output_url or "unknown", latency_ms),
        }

    def _command(self, action: str, job: RenderJob, *, wait: bool) -> list[str]:
        command = [self.executable, "generate", action, job.model, "--prompt", job.prompt]
        command.extend(("--aspect_ratio", job.aspect_ratio, "--duration", str(job.duration_seconds)))
        if job.model == "minimax_h3":
            width, height = self._dimensions(job.aspect_ratio)
            command.extend(("--resolution", "2K", "--width", str(width), "--height", str(height)))
        elif job.model == "seedance_2_0":
            command.extend(("--resolution", "720p"))
        if wait:
            command.extend(("--wait", "--wait-timeout", "30m"))
        command.append("--json")
        return command

    @staticmethod
    def _dimensions(aspect_ratio: str) -> tuple[int, int]:
        return {
            "9:16": (1080, 1920),
            "16:9": (1920, 1080),
            "1:1": (1440, 1440),
        }[aspect_ratio]

    @staticmethod
    def _credits(raw: str) -> float:
        value = json.loads(raw)
        if isinstance(value, list) and value:
            value = value[0]
        if not isinstance(value, dict):
            raise ValueError("Higgsfield cost response must be JSON")
        for key in ("credits_exact", "credits", "credit_cost", "estimated_credits", "cost"):
            candidate = value.get(key)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool) and candidate > 0:
                return float(candidate)
        raise ValueError("Higgsfield cost response did not include credits")

    @staticmethod
    def _find_url(value: Mapping[str, Any]) -> str | None:
        for key in ("url", "video_url", "output_url", "result_url"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith(("https://", "http://")):
                return candidate
        return None

    def _receipt(self, job: RenderJob, output_identifier: str, latency_ms: float) -> dict[str, Any]:
        return {
            "provider_call_id": f"video_{job.job_id}",
            "connector_session_id": job.session_id,
            "provider": self.provider_name,
            "model": job.model,
            "status": "success",
            "provenance": "live_provider",
            "estimated_credits": job.estimated_credits,
            "latency_ms": latency_ms,
            "prompt_hash": job.prompt_hash,
            "output_hash": hash_value(output_identifier),
            "raw_prompt_stored": False,
            "raw_output_stored": False,
        }


class MiniMaxH3EndpointRenderer:
    provider_name = "minimax_h3_endpoint"

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self.environment = dict(environment if environment is not None else os.environ)
        self.base_url = self.environment.get("MINIMAX_H3_BASE_URL", "").rstrip("/")
        self.token = self.environment.get("MINIMAX_H3_API_TOKEN", "")
        self.jurisdiction = self.environment.get("TD_STUDIO_JURISDICTION", "US").strip().upper()
        self.authorization_id = self.environment.get("MINIMAX_H3_LICENSE_AUTHORIZATION_ID", "").strip()

    def status(self) -> dict[str, Any]:
        license_ready = self.jurisdiction not in EXCLUDED_H3_TERRITORIES or bool(self.authorization_id)
        return {
            "configured": bool(self.base_url),
            "license_ready": license_ready,
            "jurisdiction": self.jurisdiction,
            "reason": None
            if license_ready
            else "MiniMax H3 open weights require separate written authorization in this jurisdiction",
        }

    def plan(self, job: RenderJob) -> RenderJob:
        status = self.status()
        if not status["license_ready"]:
            raise PermissionError(str(status["reason"]))
        if not status["configured"]:
            raise RuntimeError("MINIMAX_H3_BASE_URL is required")
        return job

    def execute(self, job: RenderJob, approval: PaidApproval) -> dict[str, Any]:
        self.plan(job)
        approval.assert_authorizes(job)
        payload = {
            "task": "t2va",
            "prompt": job.prompt,
            "conditions": [],
            "target": {
                "short_edge": job.short_edge,
                "aspect_ratio": job.aspect_ratio,
                "duration_seconds": job.duration_seconds,
            },
            "seed": 0,
        }
        response = self._request("POST", "/v1/videos", payload)
        video_id = response.get("id")
        if not isinstance(video_id, str) or not video_id:
            raise ValueError("MiniMax H3 endpoint response missing id")
        return {
            "result": {
                "status": "submitted",
                "provider": self.provider_name,
                "video_id": video_id,
                "status_url": f"{self.base_url}/v1/videos/{urllib.parse.quote(video_id, safe='')}",
                "content_url": f"{self.base_url}/v1/videos/{urllib.parse.quote(video_id, safe='')}/content",
            },
            "receipt": {
                "provider_call_id": f"video_{job.job_id}",
                "connector_session_id": job.session_id,
                "provider": self.provider_name,
                "model": job.model,
                "status": "submitted",
                "provenance": "live_provider",
                "prompt_hash": job.prompt_hash,
                "output_hash": hash_value(video_id),
                "raw_prompt_stored": False,
                "raw_output_stored": False,
            },
        }

    def _request(self, method: str, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        headers = {"content-type": "application/json", "accept": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            value = json.loads(response.read().decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("MiniMax H3 endpoint returned non-object JSON")
        return value
