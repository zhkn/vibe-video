"""
安全测试 — 路径穿越 + 并发锁 + 错误码
"""
import os
import sys
import threading
import time

# 添加 app 到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

# 设置测试用的 ROOT_DIR
os.environ.setdefault("XIAOMI_API_KEY", "test")
os.environ.setdefault("XIAOMI_BASE_URL", "http://test")

from app.server import (
    resolve_project_dir, resolve_file_path,
    acquire_project_lock, release_project_lock, project_locks,
    app,
)


# ─── 路径穿越测试 ───

class TestPathTraversal:
    """test_project_id_path_traversal"""

    def test_normal_project_id(self, tmp_path):
        """正常 project_id 应该成功解析"""
        import app.server as srv
        old_root = srv.ROOT_DIR
        srv.ROOT_DIR = str(tmp_path)
        try:
            # 创建目录
            os.makedirs(tmp_path / "2026-06-07" / "garden", exist_ok=True)
            result = resolve_project_dir("2026-06-07/garden")
            assert str(tmp_path / "2026-06-07" / "garden") == result
        finally:
            srv.ROOT_DIR = old_root

    def test_dotdot_traversal_rejected(self, tmp_path):
        """../ 路径穿越应该被拒绝"""
        import app.server as srv
        old_root = srv.ROOT_DIR
        srv.ROOT_DIR = str(tmp_path)
        try:
            with pytest.raises(ValueError, match="非法|越界"):
                resolve_project_dir("../../etc/passwd")
        finally:
            srv.ROOT_DIR = old_root

    def test_absolute_path_rejected(self, tmp_path):
        """绝对路径应该被拒绝"""
        import app.server as srv
        old_root = srv.ROOT_DIR
        srv.ROOT_DIR = str(tmp_path)
        try:
            with pytest.raises(ValueError, match="非法|越界"):
                resolve_project_dir("/etc/passwd")
        finally:
            srv.ROOT_DIR = old_root

    def test_encoded_traversal_rejected(self, tmp_path):
        """编码的路径穿越应该被拒绝"""
        import app.server as srv
        old_root = srv.ROOT_DIR
        srv.ROOT_DIR = str(tmp_path)
        try:
            with pytest.raises(ValueError, match="非法|越界"):
                resolve_project_dir("2026-06-07/../../../etc")
        finally:
            srv.ROOT_DIR = old_root

    def test_file_path_filename_injection(self, tmp_path):
        """文件名中包含 ../ 应该被拒绝"""
        import app.server as srv
        old_root = srv.ROOT_DIR
        srv.ROOT_DIR = str(tmp_path)
        try:
            os.makedirs(tmp_path / "2026-06-07" / "proj" / "raw", exist_ok=True)
            with pytest.raises(ValueError, match="非法文件名"):
                resolve_file_path("2026-06-07/proj", "raw", "../../../etc/passwd")
        finally:
            srv.ROOT_DIR = old_root

    def test_file_path_normal(self, tmp_path):
        """正常文件名应该成功"""
        import app.server as srv
        old_root = srv.ROOT_DIR
        srv.ROOT_DIR = str(tmp_path)
        try:
            raw_dir = tmp_path / "2026-06-07" / "proj" / "raw"
            raw_dir.mkdir(parents=True)
            (raw_dir / "test.mp4").touch()
            result = resolve_file_path("2026-06-07/proj", "raw", "test.mp4")
            assert result.endswith("test.mp4")
        finally:
            srv.ROOT_DIR = old_root


# ─── 并发锁测试 ───

class TestProjectLock:
    """test_full_pipeline_project_lock"""

    def setup_method(self):
        """清理锁"""
        project_locks.clear()

    def test_acquire_first_time_succeeds(self):
        """第一次获取锁应该成功"""
        assert acquire_project_lock("test-project") is True

    def test_acquire_second_time_fails(self):
        """第二次获取锁应该失败"""
        acquire_project_lock("test-project")
        assert acquire_project_lock("test-project") is False

    def test_release_allows_reacquire(self):
        """释放后应该能重新获取"""
        acquire_project_lock("test-project")
        release_project_lock("test-project")
        assert acquire_project_lock("test-project") is True

    def test_different_projects_independent(self):
        """不同项目的锁应该独立"""
        acquire_project_lock("project-a")
        assert acquire_project_lock("project-b") is True

    def test_concurrent_access(self):
        """并发获取同一项目锁，只有一个成功"""
        results = []
        def try_acquire():
            results.append(acquire_project_lock("concurrent-test"))

        threads = [threading.Thread(target=try_acquire) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results) == 1  # 只有一个成功

    def test_full_pipeline_returns_409(self):
        """全流程 API 在锁冲突时返回 409"""
        acquire_project_lock("2026-06-07/test")

        with app.test_client() as client:
            resp = client.post("/api/projects/2026-06-07/test/full-pipeline",
                             json={"theme": "test", "duration": 60})
            assert resp.status_code == 409
            data = resp.get_json()
            assert data["error"]["code"] == "PIPELINE_RUNNING"

        release_project_lock("2026-06-07/test")


# ─── 错误码测试 ───

class TestErrorCodes:

    def test_project_not_found_returns_404(self):
        """不存在的项目返回 404"""
        with app.test_client() as client:
            resp = client.get("/api/projects/nonexistent/project")
            assert resp.status_code == 404
            data = resp.get_json()
            assert "error" in data
            assert data["error"]["code"] == "PROJECT_NOT_FOUND"

    def test_error_response_format(self):
        """错误响应应该有统一格式"""
        from app.server import error_response
        with app.test_client() as client:
            with app.test_request_context():
                resp = error_response("TEST_CODE", "test message", stage="test", status=422)
                # error_response 返回 (response, status_code)
                assert resp[1] == 422
