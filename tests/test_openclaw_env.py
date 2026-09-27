import httpx
import pytest
from scripts.openclaw_env import check_api, environment


def test_local_key_overrides_stale_environment_without_shell_execution(tmp_path):
    path = tmp_path / '.env'
    path.write_text('OPENAI_API_KEY=synthetic-local\nPATH=malicious\nDATABASE_PATH=${HOME}/literal\n')
    env = environment(path, {'OPENAI_API_KEY': 'synthetic-stale', 'PATH': '/usr/bin'})
    assert env['OPENAI_API_KEY'] == 'synthetic-local'
    assert env['PATH'] == '/usr/bin'
    assert env['DATABASE_PATH'] == '${HOME}/literal'


def test_hosted_environment_without_file():
    assert environment(None, {'OPENAI_API_KEY': 'synthetic'})['OPENAI_API_KEY'] == 'synthetic'
    with pytest.raises(ValueError, match='missing'):
        environment(None, {})


def test_api_error_does_not_leak_key(monkeypatch):
    def post(url, **kwargs):
        assert url == 'https://api.openai.com/v1/responses'
        assert kwargs['json']['store'] is False
        return httpx.Response(401, json={'error': {'code': 'invalid_api_key', 'message': 'secret-key-fragment'}})
    monkeypatch.setattr(httpx, 'post', post)
    result, code = check_api({'OPENAI_API_KEY': 'synthetic-secret'}, 'gpt-4.1-mini')
    assert code == 1
    assert result == {'ok': False, 'http_status': 401, 'error': 'invalid_api_key'}
