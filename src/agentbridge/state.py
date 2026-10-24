import os
from pathlib import Path
from uuid import UUID

from .storage import private_dir
from .supervisor import socket_path


def root():
    return private_dir(Path(os.environ.get('AGENTBRIDGE_STATE', Path.home() / '.local/state/agentbridge')))


def execution(execution_id):
    return private_dir(root() / 'executions' / str(UUID(execution_id)))


def address(execution_id):
    return socket_path(execution(execution_id) / 'control.sock')
