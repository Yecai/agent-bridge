import base64
import json
from uuid import uuid4

from agentbridge.remote import RemoteExecution
from agentbridge.storage import write_json


async def test_transcript_mirror_retains_tail_and_rejects_foreign_path(tmp_path,monkeypatch):
    profile=tmp_path/'claude'
    projects=profile/'projects'
    projects.mkdir(parents=True)
    monkeypatch.setenv('CLAUDE_CONFIG_DIR',str(profile))
    session=str(uuid4())
    transcript=projects/(session+'.jsonl')
    transcript.write_bytes(b'{"type":"assistant","message":{"id":"m1"}}\n{"type":')
    root=tmp_path/'execution'
    remote=RemoteExecution(root,'execution',0)
    remote.row['session_id']=session
    write_json(root/'session.json',{'session_id':session,'transcript_path':str(transcript)})
    first=await remote.handle({'action':'events','cursor':0})
    lines=base64.b64decode(first['transcript']).splitlines()
    assert len(lines)==1 and json.loads(lines[0])['data']['message']['id']=='m1'
    with transcript.open('ab') as stream:
        stream.write(b'"user"}\n')
    second=await remote.handle({'action':'events','cursor':first['next_cursor']})
    assert json.loads(base64.b64decode(second['transcript']))['data']['type']=='user'
    outside=tmp_path/'outside.jsonl';outside.write_text('{"type":"assistant"}\n')
    write_json(root/'session.json',{'session_id':session,'transcript_path':str(outside)})
    third=await remote.handle({'action':'events','cursor':second['next_cursor']})
    assert not base64.b64decode(third['transcript'])
    assert third['transcript_gap']=='path_rejected'
