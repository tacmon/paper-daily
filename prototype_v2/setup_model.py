#!/usr/bin/env python3
"""Explicit, pinned model download; normal search never uses the network."""
import argparse
import hashlib
import json
from pathlib import Path
import time
import urllib.request

HERE = Path(__file__).resolve().parent
MODEL_DIR = HERE.parent / '.runtime' / 'semantic-model'


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def install(directory=MODEL_DIR, proxy=None):
    spec = json.loads((HERE / 'model.lock.json').read_text())
    directory.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http': proxy, 'https': proxy} if proxy else {}))
    for item in spec['files']:
        target = directory / item['path']
        if target.exists() and sha256(target) == item['sha256']:
            print('Already verified:', item['path'], flush=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://huggingface.co/{spec['repo']}/resolve/{spec['revision']}/{item['path']}"
        part = target.with_suffix(target.suffix + '.part')
        start = time.monotonic()
        size = 0
        with opener.open(url, timeout=45) as response, part.open('wb') as out:
            while block := response.read(1024 * 1024):
                size += len(block)
                if size > item['size'] or time.monotonic() - start > 600:
                    raise RuntimeError('Model download exceeded its size/time budget')
                out.write(block)
        if size != item['size'] or sha256(part) != item['sha256']:
            raise RuntimeError('Model file integrity check failed: ' + item['path'])
        part.replace(target)
        print(f"Verified {item['path']}: {size} bytes, {time.monotonic()-start:.1f}s", flush=True)
    (directory / 'manifest.json').write_text(json.dumps(spec, indent=2) + '\n')
    print('Model ready:', directory, flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--proxy')
    p.add_argument('--model-dir', type=Path, default=MODEL_DIR)
    args = p.parse_args()
    install(args.model_dir, args.proxy)
