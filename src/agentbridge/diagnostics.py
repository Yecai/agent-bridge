import json
import os
import zipfile
from pathlib import Path

from .storage import file_lock


def export(log_dir, destination):
    log_dir, destination = Path(log_dir), Path(destination)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream, zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        with file_lock(log_dir / '.audit.lock'):
            for path in sorted(log_dir.glob('trace*.jsonl')):
                if not path.is_symlink():
                    archive.write(path, path.name)
        artifact_root = log_dir / 'artifacts'
        with file_lock(artifact_root / '.lock'):
            manifest_path = artifact_root / 'manifest.json'
            manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {'streams': {}, 'artifacts': {}}
            archive.writestr('artifacts/manifest.json', json.dumps(manifest))
            for identifier, item in manifest['artifacts'].items():
                if len(identifier) != 64 or item['file'] != identifier + '.bin':
                    raise ValueError('invalid manifest')
                path = artifact_root / item['file']
                if path.is_symlink():
                    raise ValueError('symlink artifact')
                archive.write(path, 'artifacts/' + path.name)
        archive.writestr('redaction-report.json', json.dumps({'known_credentials': 'redacted before persistence',
                            'unknown_secrets': 'not guaranteed', 'journal_included': False}))
    return destination.resolve()
