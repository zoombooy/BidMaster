"""A2A 协议端到端测试：能力发现 → message/send → 轮询 tasks/get → 取消语义。

其他 Agent 平台（LangChain/CrewAI/Coze 等）正是按这条链路调用 BidMaster。
"""
from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.make_sample import build_sample_docx  # noqa: E402


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from bidmaster.api.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def sample_b64(tmp_path_factory):
    work = tmp_path_factory.mktemp("a2a")
    docx = build_sample_docx(work / "sample_tender.docx")
    return base64.b64encode(docx.read_bytes()).decode()


def _rpc(client, method, params, rpc_id=1):
    return client.post("/a2a", json={"jsonrpc": "2.0", "id": rpc_id,
                                     "method": method, "params": params})


class TestAgentCard:
    def test_discovery(self, client):
        r = client.get("/.well-known/agent.json")
        assert r.status_code == 200
        card = r.json()
        assert card["name"]
        assert card["protocolVersion"].startswith("0.2")
        assert card["capabilities"]["streaming"] is True
        assert card["url"].endswith("/a2a")
        skill = card["skills"][0]
        assert skill["id"] == "tender_analysis"
        assert "application/pdf" in skill["inputModes"]

    def test_agent_card_json_alias(self, client):
        assert client.get("/.well-known/agent-card.json").status_code == 200


class TestMessageSend:
    def test_full_flow(self, client, sample_b64):
        message = {
            "role": "user", "kind": "message",
            "messageId": "msg-test-001",
            "parts": [{"kind": "file", "file": {
                "name": "tender.docx",
                "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "bytes": sample_b64}}],
        }
        r = _rpc(client, "message/send", {"message": message})
        assert r.status_code == 200
        task = r.json()["result"]
        assert task["kind"] == "task"
        assert task["status"]["state"] in ("submitted", "working", "completed")
        task_id = task["id"]

        # 轮询至终态（管线在后台线程执行）
        deadline = time.time() + 90
        state, artifacts = None, []
        while time.time() < deadline:
            got = _rpc(client, "tasks/get", {"id": task_id}).json()["result"]
            state = got["status"]["state"]
            artifacts = got["artifacts"]
            if state in ("completed", "failed"):
                break
            time.sleep(0.5)
        assert state == "completed", f"任务未完成: {state}"
        assert artifacts and artifacts[0]["name"] == "tender_report.json"
        data_part = artifacts[0]["parts"][0]
        assert data_part["kind"] == "data"
        report = data_part["data"]
        assert report["stats"]["score_items"] == 10
        assert report["fields"]["price_limit"]["value_normalized"] == "250000000"

    def test_missing_file_fails_gracefully(self, client):
        message = {"role": "user", "kind": "message", "messageId": "msg-x",
                   "parts": [{"kind": "text", "text": "解析一下"}]}
        r = _rpc(client, "message/send", {"message": message})
        task = r.json()["result"]
        deadline = time.time() + 30
        while time.time() < deadline:
            got = _rpc(client, "tasks/get", {"id": task["id"]}).json()["result"]
            if got["status"]["state"] in ("completed", "failed"):
                break
            time.sleep(0.3)
        assert got["status"]["state"] == "failed"
        assert "未找到招标文件" in got["status"]["message"]["parts"][0]["text"]

    def test_cancel_completed_task_rejected(self, client):
        r = _rpc(client, "tasks/cancel", {"id": "nonexistent"})
        assert r.json()["error"]["code"] == -32001


class TestProtocolErrors:
    def test_method_not_found(self, client):
        r = _rpc(client, "foo/bar", {})
        assert r.json()["error"]["code"] == -32601

    def test_missing_message(self, client):
        r = _rpc(client, "message/send", {})
        assert r.json()["error"]["code"] == -32602

    def test_push_config(self, client):
        # 未注册任务 → TASK_NOT_FOUND
        r = _rpc(client, "tasks/pushNotificationConfig/set", {
            "taskId": "nope",
            "pushNotificationConfig": {"url": "http://localhost/hook"}})
        assert r.json()["error"]["code"] == -32001
