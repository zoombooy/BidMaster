"""轻量 JSON 持久层：工作区文件存储（dev 默认）。

生产可切换 PostgreSQL（schema 见 docs/，一期接口保持不变）。
"""
from __future__ import annotations

import json
from pathlib import Path


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def workspace_for(work_root: Path, doc_id: str) -> Path:
    ws = Path(work_root) / doc_id
    ws.mkdir(parents=True, exist_ok=True)
    return ws
