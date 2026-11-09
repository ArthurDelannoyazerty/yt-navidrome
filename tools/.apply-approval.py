"""Apply the exact locally tested source changes, checking every input and output."""
import base64
import hashlib
import json
from pathlib import Path
import zlib

allowed = {
    'README.md', 'docs/continuous-review.md', 'docs/interface.md',
    'docs/metadata-review.md', 'src/metadata_routes.py', 'src/review_routes.py',
    'src/static/app.js', 'src/static/index.html', 'src/static/metadata-ui.js',
    'src/static/style.css', 'tests/browser_ui.test.cjs', 'tests/frontend_harness.cjs',
    'tests/frontend_runtime.test.cjs', 'tests/metadata_frontend.test.cjs',
    'tests/test_frontend.py', 'tests/test_review_queue.py',
}
payload = b''.join(Path(f'tools/.approval-upload-{n:02d}').read_bytes() for n in range(1, 17))
assert hashlib.sha256(payload).hexdigest() == '69214729247e9f3c8aa7f2f71aa6cff9d5719c524e4dca60c895006ae7154f86'
changes = json.loads(zlib.decompress(base64.b64decode(payload, validate=True)))
assert len(changes) == len(allowed) and {c['path'] for c in changes} == allowed
pending = []
for change in changes:
    path = Path(change['path'])
    original = path.read_bytes()
    assert hashlib.sha256(original).hexdigest() == change['before'], str(path)
    lines = original.decode('utf-8').splitlines(True)
    previous = len(lines) + 1
    for start, end, text in reversed(change['edits']):
        assert 0 <= start <= end <= previous
        lines[start:end] = [text]
        previous = start
    result = ''.join(lines).encode('utf-8')
    assert hashlib.sha256(result).hexdigest() == change['after'], str(path)
    pending.append((path, result))
for path, result in pending:
    path.write_bytes(result)
print('Verified all 16 original files and all 16 resulting files.')
