from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("touchdown_studio_server", ROOT / "server.py")
assert SPEC and SPEC.loader
server_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = server_module
SPEC.loader.exec_module(server_module)
import renderers


class FakeTransport:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((method, path, body))
        return self.responses.pop(0)


def request(url: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"content-type": "application/json"} if payload is not None else {}
    method = "POST" if payload is not None else "GET"
    with urllib.request.urlopen(urllib.request.Request(url + path, data=data, headers=headers, method=method), timeout=8) as response:
        return json.loads(response.read().decode())


class StudioTests(unittest.TestCase):
    def service(
        self,
        environment: dict[str, str] | None = None,
        transport: FakeTransport | None = None,
    ) -> Any:
        values = dict(environment or {})
        values["TD_STUDIO_DB_PATH"] = ":memory:"
        return server_module.StudioService(environment=values, transport=transport)

    def test_assets_and_server_have_no_inferguard_dependency(self) -> None:
        combined = "\n".join(
            (ROOT / name).read_text()
            for name in ("server.py", "renderers.py", "index.html", "app.js", "styles.css")
        )
        self.assertIn("Touchdown AI Video Studio", combined)
        self.assertIn('/api/render/fake', combined)
        self.assertNotIn("td_inferguard", combined)
        self.assertNotIn("inferguard/", combined.lower())

    def test_fixture_path_returns_storyboard_and_hash_only_receipt(self) -> None:
        service = self.service()
        created = service.create_session({
            "harness": "fixture_harness",
            "control_provider": "glm_zai_api",
            "objective": "PRIVATE OBJECTIVE",
            "acceptance_contract": "Six shots and a CTA",
        })
        turn = service.turn({
            "session": created["session"],
            "message": "PRIVATE MODEL PROMPT",
            "brief": {"format": "startup_launch", "product": "Touchdown", "audience": "founders", "objective": "ship"},
        })
        render = service.fake_render({
            "task_id": created["work"]["work_id"],
            "session_id": created["session"]["session_id"],
            "prompt": "PRIVATE VIDEO PROMPT",
            "aspect_ratio": "9:16",
            "duration_seconds": 6,
        })
        self.assertIn("FIXTURE STORYBOARD", turn["events"][1]["text"])
        self.assertFalse(render["result"]["produced_video_pixels"])
        self.assertEqual(0.0, render["receipt"]["estimated_cost_usd"])
        self.assertNotIn("PRIVATE VIDEO PROMPT", json.dumps(render))

    def test_trueforge_maps_control_provider_to_named_agent(self) -> None:
        transport = FakeTransport([
            {"data": {"id": "session-1"}},
            {"data": {"id": "turn-1"}},
            {"data": [{"id": "event-1", "event": {"type": "model.message", "content": "Storyboard"}}]},
        ])
        service = self.service(
            environment={
                "TRUEFORGE_BASE_URL": "http://127.0.0.1:8790",
                "TD_STUDIO_TRUEFORGE_AGENTS": '{"glm_zai_api":"touchdown-video-glm"}',
            },
            transport=transport,
        )
        created = service.create_session({
            "harness": "trueforge_http",
            "control_provider": "glm_zai_api",
            "objective": "Launch video",
            "acceptance_contract": "Six shots",
        })
        turn = service.turn({"session": created["session"], "message": "Build it"})
        self.assertEqual({"agent": {"name": "touchdown-video-glm"}}, transport.calls[0][2])
        self.assertEqual("assistant.delta", turn["events"][0]["type"])
        self.assertEqual("Storyboard", turn["events"][0]["text"])

    def test_sqlite_persists_project_storyboard_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "studio.sqlite3")
            service = server_module.StudioService(environment={"TD_STUDIO_DB_PATH": database_path})
            created = service.create_session({
                "harness": "fixture_harness",
                "control_provider": "glm_zai_api",
                "objective": "Launch video",
                "acceptance_contract": "Six shots",
            })
            work_id = created["work"]["work_id"]
            service.turn({
                "session": created["session"],
                "message": "PRIVATE MODEL PROMPT",
                "brief": {"format": "startup_launch", "product": "Touchdown", "audience": "founders"},
            })
            service.fake_render({
                "task_id": work_id,
                "session_id": created["session"]["session_id"],
                "prompt": "PRIVATE VIDEO PROMPT",
            })
            service.store.close()

            reopened = server_module.StudioService(environment={"TD_STUDIO_DB_PATH": database_path})
            saved = reopened.project(work_id)["project"]
            self.assertEqual("fixture", saved["status"])
            self.assertIn("FIXTURE STORYBOARD", saved["storyboard"])
            self.assertEqual("fake_video", saved["latest_render"]["job"]["provider"])
            self.assertNotIn("PRIVATE MODEL PROMPT", json.dumps(saved))
            self.assertNotIn("PRIVATE VIDEO PROMPT", json.dumps(saved))
            reopened.store.close()

    def test_higgsfield_plan_and_exact_approval_are_offline_testable(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **kwargs: Any) -> Any:
            calls.append(command)
            return type("Completed", (), {"returncode": 0, "stdout": '{"credits":22.5}', "stderr": ""})()

        renderer = renderers.HiggsfieldRenderer(run=fake_run)
        job = renderers.RenderJob(
            job_id="video-1",
            task_id="task-1",
            session_id="session-1",
            provider="higgsfield_cli",
            model="seedance_2_0",
            prompt="PRIVATE PROMPT",
            aspect_ratio="9:16",
            duration_seconds=5,
        )
        planned = renderer.plan(job)
        self.assertEqual(22.5, planned.estimated_credits)
        self.assertIn("--resolution", calls[0])
        self.assertNotIn("PRIVATE PROMPT", json.dumps(planned.safe_json()))
        approval = renderers.PaidApproval(
            provider="higgsfield_cli",
            job_approval_hash=planned.approval_hash,
            maximum_credits=22.5,
        )
        approval.assert_authorizes(planned)

    def test_higgsfield_status_timeout_does_not_break_studio_status(self) -> None:
        def timed_out(command: list[str], **kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 5))

        renderer = renderers.HiggsfieldRenderer(executable=sys.executable, run=timed_out)
        status = renderer.status()
        self.assertTrue(status["installed"])
        self.assertFalse(status["authenticated"])
        self.assertIn("status check failed", status["reason"])

    def test_minimax_h3_endpoint_fails_closed_in_us_without_authorization(self) -> None:
        renderer = renderers.MiniMaxH3EndpointRenderer(
            {"MINIMAX_H3_BASE_URL": "http://127.0.0.1:30010", "TD_STUDIO_JURISDICTION": "US"}
        )
        job = renderers.RenderJob(
            job_id="video-h3",
            task_id="task-1",
            session_id="session-1",
            provider="minimax_h3_endpoint",
            model="MiniMaxAI/MiniMax-H3",
            prompt="Video prompt",
        )
        with self.assertRaisesRegex(PermissionError, "written authorization"):
            renderer.plan(job)

    def test_http_smoke(self) -> None:
        httpd = server_module.create_server(port=0, service=self.service())
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            status = request(url, "/api/status")
            self.assertTrue(status["ok"])
            self.assertFalse(status["inferguard_dependency"])
            self.assertEqual("sqlite", status["storage"]["type"])
            self.assertFalse(status["storage"]["persistent"])
            with urllib.request.urlopen(url + "/", timeout=3) as response:
                self.assertIn("Touchdown AI Video Studio", response.read().decode())
                self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
