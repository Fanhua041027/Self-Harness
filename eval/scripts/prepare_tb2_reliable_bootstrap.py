#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path


MARKER = "# Self-Harness local dependency bootstrap reliability"
BOOTSTRAP = r'''# Self-Harness local dependency bootstrap reliability
if [ -d /etc/apt/apt.conf.d ]; then
  printf 'Acquire::Retries "5";\nAcquire::http::Timeout "30";\n' > /etc/apt/apt.conf.d/80-self-harness-retries
fi
for sources_file in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
  [ -f "$sources_file" ] || continue
  sed -i \
    -e 's|http://deb.debian.org/debian-security|http://mirrors.tuna.tsinghua.edu.cn/debian-security|g' \
    -e 's|http://security.debian.org/debian-security|http://mirrors.tuna.tsinghua.edu.cn/debian-security|g' \
    -e 's|http://deb.debian.org/debian|http://mirrors.tuna.tsinghua.edu.cn/debian|g' \
    -e 's|http://archive.ubuntu.com/ubuntu|http://mirrors.tuna.tsinghua.edu.cn/ubuntu|g' \
    -e 's|http://security.ubuntu.com/ubuntu|http://mirrors.tuna.tsinghua.edu.cn/ubuntu|g' \
    "$sources_file"
done
printf 'retry = 5\nretry-delay = 2\nconnect-timeout = 30\n' > /root/.curlrc
export UV_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"
if [ -f /tests/.self-harness-bin/uv ] && [ -f /tests/.self-harness-bin/uvx ]; then
  mkdir -p /root/.local/bin
  cp /tests/.self-harness-bin/uv /root/.local/bin/uv
  cp /tests/.self-harness-bin/uvx /root/.local/bin/uvx
  chmod +x /root/.local/bin/uv /root/.local/bin/uvx
  printf 'export PATH="$HOME/.local/bin:$PATH"\n' > /root/.local/bin/env
fi
'''


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a TB2 task root with reliable dependency bootstrap.")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--uv", required=True, type=Path)
    parser.add_argument("--uvx", required=True, type=Path)
    args = parser.parse_args()

    source = args.source.resolve()
    output = args.output.resolve()
    uv = args.uv.resolve()
    uvx = args.uvx.resolve()
    if output.exists():
        raise RuntimeError(f"output already exists: {output}")
    shutil.copytree(source, output)

    changes = []
    for path in sorted(output.rglob("test.sh")):
        text = path.read_text(encoding="utf-8")
        if MARKER in text:
            continue
        before = sha256(text.encode())
        if text.startswith("#!"):
            first, separator, rest = text.partition("\n")
            updated = first + separator + "\n" + BOOTSTRAP + "\n" + rest
        else:
            updated = BOOTSTRAP + "\n" + text
        updated = updated.replace(
            "curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh",
            '[ -x "$HOME/.local/bin/uv" ] || curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh',
        )
        path.write_text(updated, encoding="utf-8", newline="\n")
        asset_dir = path.parent / ".self-harness-bin"
        asset_dir.mkdir(exist_ok=True)
        link_or_copy(uv, asset_dir / "uv")
        link_or_copy(uvx, asset_dir / "uvx")
        changes.append(
            {
                "path": path.relative_to(output).as_posix(),
                "before_sha256": before,
                "after_sha256": sha256(updated.encode()),
            }
        )

    manifest = {
        "source": str(source),
        "output": str(output),
        "change_scope": "dependency bootstrap only; task instructions and test assertions unchanged",
        "uv_source": str(uv),
        "uv_sha256": sha256(uv.read_bytes()),
        "uvx_source": str(uvx),
        "uvx_sha256": sha256(uvx.read_bytes()),
        "changed_test_scripts": len(changes),
        "changes": changes,
    }
    (output / "SELF_HARNESS_BOOTSTRAP_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"prepared {output} ({len(changes)} test.sh files patched)")
    return 0


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def link_or_copy(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


if __name__ == "__main__":
    raise SystemExit(main())
