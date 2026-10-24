import asyncio
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from .errors import BridgeError

# Constant program. All variable data is read as JSON on stdin.
BOOTSTRAP = r'''
import hashlib,json,os,pathlib,re,subprocess,sys,venv,zipfile,stat
request=json.load(sys.stdin)
if sys.version_info < (3,11):
    sys.exit('python_unsupported')
wheel=pathlib.Path(request['wheel'])
digest=request['sha256']
if not re.fullmatch('[a-f0-9]{64}',digest) or hashlib.sha256(wheel.read_bytes()).hexdigest()!=digest:
    sys.exit('digest_mismatch')
root=pathlib.Path(request['home'])/'runtime'/digest
root.mkdir(mode=0o700,parents=True,exist_ok=True)
os.chmod(root,0o700)
python=root/'venv/bin/python'
if not (root/'ready.json').exists():
    venv.EnvBuilder(with_pip=False,symlinks=True).create(root/'venv')
    # Remote supervision is pure Python + stdlib. Install this verified wheel into
    # its private venv without requiring Debian's optional ensurepip/system pip.
    purelib=pathlib.Path(subprocess.check_output([str(python),'-c','import sysconfig;print(sysconfig.get_path("purelib"))'],text=True).strip())
    with zipfile.ZipFile(wheel) as archive:
        for item in archive.infolist():
            name=pathlib.PurePosixPath(item.filename)
            if name.is_absolute() or '..' in name.parts or stat.S_ISLNK(item.external_attr >> 16):
                sys.exit('invalid_wheel_member')
            if not item.is_dir():
                destination=purelib.joinpath(*name.parts)
                destination.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                destination.write_bytes(archive.read(item))
                destination.chmod(0o600)
    subprocess.run([str(python),'-c','import agentbridge.remote,agentbridge.hooks'],check=True)
    marker=root/'ready.json'
    marker.write_text(json.dumps({'sha256':digest}))
    marker.chmod(0o600)
print(json.dumps({'python':str(python),'sha256':digest,'root':str(root)}))
'''


def wheel_path():
    explicit = os.environ.get('AGENTBRIDGE_WHEEL')
    if explicit:
        path = Path(explicit).resolve(strict=True)
        if path.suffix != '.whl':
            raise BridgeError('invalid_wheel', '部署文件必须是 wheel')
        return path
    project = Path(__file__).resolve().parents[2]
    if not (project / 'pyproject.toml').is_file():
        raise BridgeError('wheel_required', '已安装发行版需通过 AGENTBRIDGE_WHEEL 指定验收 wheel')
    directory = Path(tempfile.mkdtemp(prefix='agentbridge-build-'))
    result = subprocess.run(['uv', 'build', '--wheel', '--out-dir', str(directory)], cwd=project,
                            capture_output=True, timeout=60)
    if result.returncode:
        raise BridgeError('build_failed', 'AgentBridge wheel 构建失败')
    return next(directory.glob('*.whl'))


async def deploy(connection, runtime=None):
    wheel = await asyncio.to_thread(wheel_path)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    async with connection.start_sftp_client() as sftp:
        home = await sftp.realpath('.')
        root = home + '/.local/share/agentbridge'
        incoming = root + '/incoming'
        await sftp.makedirs(incoming, exist_ok=True)
        await sftp.chmod(root, 0o700)
        await sftp.chmod(incoming, 0o700)
        target = incoming + '/' + digest + '-' + wheel.name
        # Wheel filename must remain valid for pip; put each digest in its own directory.
        directory = incoming + '/' + digest
        await sftp.makedirs(directory, exist_ok=True)
        await sftp.chmod(directory, 0o700)
        target = directory + '/' + wheel.name
        await sftp.put(str(wheel), target + '.part')
        await sftp.chmod(target + '.part', 0o600)
        await sftp.posix_rename(target + '.part', target)
    process = await connection.create_process('python3 -c ' + shlex.quote(BOOTSTRAP))
    stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps({'wheel': target, 'sha256': digest, 'home': root})), 120)
    if runtime is not None:
        refs = runtime.artifacts.put(json.dumps({'stdout': stdout, 'stderr': stderr}).encode())
        runtime.audit.append('remote.bootstrap', 'ok' if process.exit_status == 0 else 'error',
                             {'exit_code': process.exit_status, 'wheel_sha256': digest}, artifact_refs=refs)
    if process.exit_status != 0:
        raise BridgeError('bootstrap_failed', '远端 Python/venv/wheel 检查失败')
    return json.loads(stdout)
