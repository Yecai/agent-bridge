"""Standalone loopback SSH fixture for another repository's CLI integration tests."""
import asyncio
import json
import os
import shlex
import sys
from pathlib import Path

import asyncssh


class Server(asyncssh.SSHServer):
    def begin_auth(self, username):
        return False

    def server_requested(self, host, port):
        return host == '127.0.0.1'


async def main():
    root=Path(sys.argv[1]).resolve()
    class SFTP(asyncssh.SFTPServer):
        def map_path(self,path):
            return path if path.startswith(b'/') else os.fsencode(root)+b'/'+path
    async def execute(channel):
        argv=shlex.split(channel.command)
        if argv[0]=='python3':argv[0]=sys.executable
        env={**os.environ,'AGENTBRIDGE_REMOTE_ROOT':str(root/'.local/share/agentbridge/executions'),'AGENTBRIDGE_FAKE_ROLES':'1'}
        proc=await asyncio.create_subprocess_exec(*argv,cwd=root,env=env,stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        data=(await channel.stdin.read()).encode()
        stdout,stderr=await proc.communicate(data)
        channel.stdout.write(stdout.decode());channel.stderr.write(stderr.decode());channel.exit(proc.returncode)
    key=asyncssh.generate_private_key('ssh-ed25519')
    server=await asyncssh.create_server(Server,'127.0.0.1',0,server_host_keys=[key],process_factory=execute,sftp_factory=SFTP)
    try:
        print(json.dumps({'port':server.get_port(),'known_host':f'[127.0.0.1]:{server.get_port()} '+key.export_public_key().decode(),
                          'claude_exe':[sys.executable,str(Path(__file__).with_name('fake_claude.py'))]}),flush=True)
        await asyncio.to_thread(sys.stdin.buffer.read)
    finally:
        server.close();await server.wait_closed()


asyncio.run(main())
