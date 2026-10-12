import sys

import httpx
import pytest
from app_helpers import flow_app as create_app

from werewolf import api_demo


@pytest.mark.asyncio
async def test_demo_ignores_proxy_environment_and_completes_game(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    app = create_app(tmp_path / "demo.sqlite3", default_playback="auto", reading_scale=0)
    original_client = httpx.AsyncClient
    observed = []

    def local_client(**kwargs):
        observed.append(kwargs["trust_env"])
        return original_client(transport=httpx.ASGITransport(app=app), **kwargs)

    monkeypatch.setattr(api_demo.httpx, "AsyncClient", local_client)
    async with app.router.lifespan_context(app):
        await api_demo.run("http://127.0.0.1:8000", 38, 3)
    assert observed == [False]
    assert "无真实 API 调用" in capsys.readouterr().out


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout, httpx.HTTPStatusError])
def test_demo_reports_request_failure_without_traceback(monkeypatch, capsys, error_type):
    async def failing_run(*args):
        request = httpx.Request("POST", "http://127.0.0.1:8000/api/games")
        if error_type is httpx.HTTPStatusError:
            raise error_type("private error", request=request, response=httpx.Response(502, request=request))
        raise error_type("private error", request=request)

    monkeypatch.setattr(api_demo, "run", failing_run)
    monkeypatch.setattr(sys, "argv", ["api-demo"])
    with pytest.raises(SystemExit) as error:
        api_demo.main()
    assert error.value.code == 1
    message = capsys.readouterr().err
    assert message
    assert "private error" not in message
    assert "Traceback" not in message
    if error_type is httpx.ConnectError:
        assert "第二个终端" in message
        assert "不要按 Ctrl+C" in message
    elif error_type is httpx.HTTPStatusError:
        assert "502" in message
