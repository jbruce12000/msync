#!/usr/bin/env python3
"""git "clean" filter that keeps Pandora credentials out of commits.

Whenever config.py is staged (git add), git pipes the file through this
filter. Any line that assigns PANDORA_USERNAME or PANDORA_PASSWORD is
rewritten to the credential-free env-var form:

    PANDORA_USERNAME = os.environ.get("MSYNC_PANDORA_USERNAME", "")
    PANDORA_PASSWORD = os.environ.get("MSYNC_PANDORA_PASSWORD", "")

The WORKING TREE keeps whatever you typed (so your real credentials work the
moment the server imports config.py); only what git stages/commits is
scrubbed. Worse-case forms (trailing comments, single quotes, odd spacing)
can't slip through: the whole line is replaced, not just the quoted value.

Installed per-clone by tools/setup-msync-secret-filter.sh (nothing is
committed or shared):

    git config filter.msync-secrets.clean "python3 .../msync-secrets-clean.py"
    git config filter.msync-secrets.smudge cat
"""
import re
import sys

_PANDORA_KEY = re.compile(r"^\s*PANDORA_(USERNAME|PASSWORD)\b.*$")
_CANONICAL = {
    "USERNAME": 'PANDORA_USERNAME = os.environ.get("MSYNC_PANDORA_USERNAME", "")',
    "PASSWORD": 'PANDORA_PASSWORD = os.environ.get("MSYNC_PANDORA_PASSWORD", "")',
}


def main() -> int:
    for line in sys.stdin:
        m = _PANDORA_KEY.match(line)
        if m:
            sys.stdout.write(_CANONICAL[m.group(1)] + "\n")
        else:
            sys.stdout.write(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())