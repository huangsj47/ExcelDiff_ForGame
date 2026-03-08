from contextlib import contextmanager
from types import SimpleNamespace

import services.repository_update_api_service as update_api


class _FakeApp:
    @contextmanager
    def app_context(self):
        yield


def test_run_repository_update_worker_records_sync_error_on_known_exception():
    repository = SimpleNamespace(id=11, name="repo-a", type="git")
    recorded_messages = []
    log_messages = []

    class _FakeSession:
        def get(self, _model, _repository_id):
            return repository

    fake_db = SimpleNamespace(session=_FakeSession())

    update_api.run_repository_update_and_cache_worker(
        repository_id=11,
        app=_FakeApp(),
        db=fake_db,
        Repository=object(),
        Commit=object(),
        get_git_service=lambda _repo: (_ for _ in ()).throw(LookupError("service lookup failed")),
        get_svn_service=lambda _repo: None,
        dispatch_auto_sync_task_when_agent_mode=lambda _repository_id: (False, None),
        clear_repository_sync_error=lambda *_args, **_kwargs: None,
        record_repository_sync_error=lambda _session, _repository, message, **_kwargs: recorded_messages.append(message),
        log_print=lambda message, *_args, **_kwargs: log_messages.append(str(message)),
    )

    assert any("异步更新异常" in message for message in recorded_messages)
    assert any("异步更新和缓存操作异常" in message for message in log_messages)
