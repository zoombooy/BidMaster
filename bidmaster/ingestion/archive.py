"""归档（zip）接入：递归解压 + GBK 文件名修复 + 压缩炸弹防护。

招标文件常以多层嵌套 zip 交付（外层包 → 招标文件.zip → 技术规范书.zip），
且由 Windows 工具打包的条目名是 GBK 编码（zipfile 默认按 cp437 解码会乱码）。
"""
from __future__ import annotations

import zipfile
from pathlib import Path

# 防压缩炸弹上限
MAX_ENTRY_SIZE = 500 * 1024 * 1024     # 单条目解压后 ≤ 500MB
MAX_TOTAL_SIZE = 2 * 1024 * 1024 * 1024  # 总解压 ≤ 2GB
MAX_ENTRIES = 2000
MAX_DEPTH = 3                          # 最多递归 3 层

DOC_EXTS = {".docx", ".pdf", ".xlsx", ".doc"}
SKIP_EXTS = {".sign", ".exe", ".dll", ".bat"}


def _fix_name(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if not (info.flag_bits & 0x800):  # 无 UTF-8 标志 → 按 GBK 修复
        try:
            name = name.encode("cp437").decode("gbk")
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
    return name.replace("\\", "/")


def extract_zip(zip_path: Path, out_dir: Path, depth: int = 0,
                _budget: list | None = None) -> list[Path]:
    """解压 zip（递归展开内层 zip/doc），返回解压出的文档文件列表。"""
    if depth > MAX_DEPTH:
        return []
    budget = _budget if _budget is not None else [0]
    docs: list[Path] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        infos = z.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ValueError(f"{zip_path.name}: 条目数 {len(infos)} 超上限，疑似压缩炸弹")
        for info in infos:
            if info.is_dir():
                continue
            name = _fix_name(info)
            budget[0] += info.file_size
            if budget[0] > MAX_TOTAL_SIZE:
                raise ValueError(f"{zip_path.name}: 解压总量超上限，疑似压缩炸弹")
            if info.file_size > MAX_ENTRY_SIZE:
                continue
            target = out_dir / Path(name).name if depth > 0 else out_dir / name
            # 路径穿越防护
            if not str(target.resolve()).startswith(str(out_dir.resolve())):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(info))

    # 分类：文档收集 / 内层归档递归 / 无关文件忽略
    for f in list(out_dir.rglob("*")):
        if not f.is_file() or f.suffix.lower() in SKIP_EXTS:
            continue
        if f.suffix.lower() == ".zip":
            inner_dir = f.parent / (f.stem[:40] + "_unzipped")
            if not inner_dir.exists():
                docs.extend(extract_zip(f, inner_dir, depth + 1, _budget=budget))
        elif f.suffix.lower() in DOC_EXTS:
            docs.append(f)
    return docs


def collect_documents(roots: list[Path]) -> list[Path]:
    """从若干目录/文件收集可解析文档（去重、按名字排序保证稳定顺序）。"""
    docs: list[Path] = []
    seen: set[Path] = set()
    for r in roots:
        if r.is_file() and r.suffix.lower() in DOC_EXTS:
            items = [r]
        elif r.is_dir():
            items = [p for p in r.rglob("*")
                     if p.is_file() and p.suffix.lower() in DOC_EXTS]
        else:
            continue
        for p in items:
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                docs.append(p)
    return sorted(docs, key=lambda p: p.name)
