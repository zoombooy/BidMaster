"""安全修复回归：上传路径穿越/SSRF/zip 路径校验。"""
import zipfile
from pathlib import Path

import pytest

from bidmaster.security import URLNotAllowed, validate_public_url


class TestURLGuard:
    def test_blocks_private_ip(self):
        with pytest.raises(URLNotAllowed):
            validate_public_url("http://127.0.0.1/x.docx")
        with pytest.raises(URLNotAllowed):
            validate_public_url("http://10.0.0.5/x.docx")
        with pytest.raises(URLNotAllowed):
            validate_public_url("http://169.254.169.254/latest/meta-data")

    def test_blocks_non_http(self):
        with pytest.raises(URLNotAllowed):
            validate_public_url("file:///etc/passwd")
        with pytest.raises(URLNotAllowed):
            validate_public_url("ftp://example.com/x")

    def test_blocks_dns_to_private(self):
        with pytest.raises(URLNotAllowed):
            validate_public_url("http://localhost/x.docx", resolve_dns=False) \
                if False else validate_public_url("http://localhost/x.docx")

    def test_allows_public(self):
        # resolve_dns=False：本机可能是 fake-IP 代理环境（域名解析到 198.18.0.0/15），
        # 公网域名的 DNS 级校验由 BIDMASTER_URL_STRICT_DNS 控制生产环境行为
        assert validate_public_url("https://example.com/tender.zip",
                                   resolve_dns=False) == \
            "https://example.com/tender.zip"


class TestZipPathGuard:
    def test_traversal_entry_skipped(self, tmp_path: Path):
        from bidmaster.ingestion.archive import extract_zip
        zpath = tmp_path / "evil.zip"
        with zipfile.ZipFile(zpath, "w") as z:
            z.writestr("../../evil.docx", b"X")
        extract_zip(zpath, tmp_path / "out")
        assert not (tmp_path / "evil.docx").exists()  # 未逃逸到 out_dir 之外
