import os
import signal
import subprocess
import time

from .errors import BridgeError


def start_marker(pid):
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True, text=True, timeout=5)
    return result.stdout.strip() if result.returncode == 0 else None


def group_alive(pgid):
    result = subprocess.run(['ps', '-Ao', 'pgid=,stat='], capture_output=True, text=True, timeout=5, check=True)
    return any(len(parts := line.split()) >= 2 and parts[0] == str(pgid) and not parts[1].startswith('Z')
               for line in result.stdout.splitlines())


def stop_group(proc, marker, grace=5):
    if proc.poll() is None and start_marker(proc.pid) != marker:
        raise BridgeError('stop_unconfirmed', '进程启动标记发生变化')
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not group_alive(proc.pid):
            break
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and group_alive(proc.pid):
            proc.poll()
            time.sleep(.05)
    proc.poll()
    if group_alive(proc.pid):
        raise BridgeError('stop_unconfirmed', '进程组停止未确认')
    return proc.wait(timeout=5)
