"""Actual CLI SIGKILL must leave the detached owners able to expire both ends."""
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

from conftest import make_spec
from agentbridge.cli import command, private_request
from agentbridge.procutil import group_alive, start_marker
from agentbridge.protocol import SshTarget


async def test_cli_sigkill_expires_lease_and_stops_remote_and_local_groups(tmp_path,monkeypatch):
    monkeypatch.setenv('AGENTBRIDGE_STATE',str(tmp_path/'local-state'))
    root=tmp_path/'remote';root.mkdir()
    env={**os.environ,'AGENTBRIDGE_FAKE_HOLD':'1'}
    server=await asyncio.create_subprocess_exec(sys.executable,str(Path(__file__).parents[1]/'fixtures/ssh_server.py'),str(root),
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=env)
    cli=None
    spec=make_spec(tmp_path)
    try:
        meta=json.loads(await asyncio.wait_for(server.stdout.readline(),20))
        hosts=tmp_path/'known_hosts';hosts.write_text(meta['known_host'])
        spec.target=SshTarget(host='127.0.0.1',port=meta['port'],user='fixture',known_hosts=hosts)
        spec.claude_exe=meta['claude_exe']
        request=tmp_path/'request.json';request.write_text(private_request(spec));request.chmod(0o600)
        with (tmp_path/'cli.out').open('wb') as output:
            cli=await asyncio.create_subprocess_exec(sys.executable,'-m','agentbridge','run','--request-file',str(request),stdout=output,stderr=output)
            deadline=time.monotonic()+45
            local_rows=[]
            while time.monotonic()<deadline:
                local_rows=[json.loads(path.read_text()) for path in (spec.log_dir/'processes').glob('*.json')]
                if local_rows:break
                await asyncio.sleep(.1)
            assert local_rows and group_alive(local_rows[0]['pid'])
            remote_status=root/'.local/share/agentbridge/executions'/spec.execution_id/'status.json'
            remote=json.loads(remote_status.read_text())
            assert group_alive(remote['pid'])
            cli.kill();await cli.wait()
            assert cli.returncode==-signal.SIGKILL
            deadline=time.monotonic()+80
            status={}
            while time.monotonic()<deadline:
                status=await command(spec.execution_id,'status')
                if status.get('stop_confirmed'):break
                await asyncio.sleep(.25)
            assert status.get('stop_confirmed'),status
            assert not group_alive(local_rows[0]['pid'])
            assert not group_alive(remote['pid'])
            assert json.loads(remote_status.read_text())['stop_confirmed']
    finally:
        if cli and cli.returncode is None:
            cli.kill();await cli.wait()
        try:
            await command(spec.execution_id,'cancel')
        except Exception:
            pass
        # Failure cleanup uses only execution-owned persistent identities.
        paths=list((spec.log_dir/'processes').glob('*.json'))+[root/'.local/share/agentbridge/executions'/spec.execution_id/'status.json']
        for path in paths:
            if path.exists():
                row=json.loads(path.read_text())
                if row.get('pid') and start_marker(row['pid'])==row.get('start_marker') and group_alive(row['pid']):
                    os.killpg(row['pid'],signal.SIGKILL)
        server.stdin.close()
        try:await asyncio.wait_for(server.wait(),10)
        except TimeoutError:server.kill();await server.wait()
