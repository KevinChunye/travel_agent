from copy import deepcopy
import json
from pathlib import Path
import stat
import pytest
from scripts.configure_openclaw import proposal, write_proposal

TEMPLATE=json.loads((Path(__file__).resolve().parents[1]/'config/openclaw/openclaw.example.json').read_text())


def test_merge_preserves_auth_channels_and_specific_routes():
    base={'gateway':{'auth':{'token':'synthetic-private'}},
          'channels':{'whatsapp':{'dmPolicy':'allowlist','allowFrom':['synthetic-owner']},'telegram':{'enabled':True}},
          'agents':{'defaults':{'model':'other-provider/model'},'list':[{'id':'main','default':True,'model':'original'}]},
          'bindings':[{'agentId':'main','match':{'channel':'whatsapp'}},
                      {'agentId':'account-special','match':{'channel':'whatsapp','accountId':'special'}}]}
    original=deepcopy(base)
    result=proposal(base,TEMPLATE,'/data/travel_agent',True,True)
    assert base==original
    assert result['gateway']==base['gateway']
    assert result['channels']==base['channels']
    assert result['agents']['defaults']['model']=='other-provider/model'
    assert result['agents']['list'][0]['model']=='original'
    assert result['bindings'][0]==base['bindings'][1]
    assert result['bindings'][-1]=={'agentId':'travel','match':{'channel':'whatsapp'}}
    assert [a['id'] for a in result['agents']['list'] if a.get('default')]==['travel']
    assert proposal(result,TEMPLATE,'/data/travel_agent',True,True)==result


def test_default_proposal_does_not_route_or_replace_current_default():
    base={'agents':{'list':[{'id':'main','default':True}]}}
    result=proposal(base,TEMPLATE,'/repo')
    assert 'channels' not in result and 'bindings' not in result
    assert [a['id'] for a in result['agents']['list'] if a.get('default')]==['main']
    assert result['agents']['list'][1]['model']['primary']=='openai/gpt-5.1'


def test_private_proposal_refuses_overwrite(tmp_path):
    output=tmp_path/'proposal.json'
    write_proposal({'private':'synthetic'},output,validate=False)
    assert stat.S_IMODE(output.stat().st_mode)==0o600
    with pytest.raises(ValueError): write_proposal({},output,validate=False)
    assert json.loads(output.read_text())=={'private':'synthetic'}


def test_validation_failure_leaves_no_output(tmp_path,monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr('scripts.configure_openclaw.subprocess.run',lambda *a,**k:SimpleNamespace(returncode=1,stderr='synthetic-private'))
    with pytest.raises(ValueError): write_proposal({},tmp_path/'bad.json')
    assert list(tmp_path.iterdir())==[]
