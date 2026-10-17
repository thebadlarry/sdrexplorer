#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Guard: the core names no module.

The core program (sdrexplorer/, without tests) must not contain the vocabulary of a module that has moved
out to its own repo, so it never expects one to be there (docs/architecture.md, design rule 2).

    python3 -m sdrexplorer.tests.test_core_generic

MOVED_OUT grows as modules move out (step 4: paging; step 5: the classifier; step 6: the digital module).
PENDING lists core files still holding one of those words because the code that uses it moves out in a
later step: each entry names that step, and must be removed when it's done (the test fails if an entry
no longer matches anything, so the list can't go stale).
"""
import os
import re
import sys

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# module -> words that may not appear in the core once it has moved out (whole words, any case)
MOVED_OUT = {
    "paging (sdrexplorer-paging)": ["pocsag", "flex", "pager", "gr-pager", "gr_pager", "paging"],
    # ("explorer" alone is the app's own name, "SDR signal explorer"; "classify" is also IP-traffic sorting)
    "classifier (sdrexplorer-classifier)": ["onnx", "onnxruntime", "explorer_tap", "explorer_dsp",
                                            "ExplorerThread", "hunt", "huntui", "hunticon", "huntverify",
                                            "classifier", "qr"],
    "digital (sdrexplorer-digital)": ["op25", "dsd", "dsd-fme", "p25", "p25-p1", "dmr", "nxdn", "nxdn48",
                                      "nxdn96", "smartnet", "edacs", "lrrp", "radioid", "mototrbo"],
}

# (core file, word): allowed until the step named
PENDING = {}

# descriptive data the core may hold (a license's emission designators, an import format's mode spellings)
ALLOWED_FILES = {"providers/fcc_uls.py", "providers/csv_import.py"}


def core_files():
    for root, dirs, files in os.walk(CORE):
        dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__", "memory", "recordings", "uls",
                                                "samples", "web")]
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(root, f)
                yield os.path.relpath(path, CORE), path


def test_core_names_no_moved_module():
    hits, used = [], set()
    for rel, path in core_files():
        if rel in ALLOWED_FILES:
            continue
        text = open(path, encoding="utf-8").read()
        for module, words in MOVED_OUT.items():
            for w in words:
                n = len(re.findall(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9])" % re.escape(w), text, re.I))
                if not n:
                    continue
                key = (rel, w)
                if key in PENDING:
                    used.add(key)
                    continue
                lines = [i + 1 for i, line in enumerate(text.splitlines())
                         if re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9])" % re.escape(w), line, re.I)]
                hits.append(f"{rel}: {w!r} ({module}) on lines {lines[:10]}")
    assert not hits, "core names a module that has moved out:\n  " + "\n  ".join(hits)
    stale = sorted(set(PENDING) - used)
    assert not stale, f"PENDING entries that no longer match anything (remove them): {stale}"
    print(f"ok  test_core_names_no_moved_module ({len(used)} pending exceptions, "
          f"{sum(len(w) for w in MOVED_OUT.values())} words)")


def main():
    test_core_names_no_moved_module()


if __name__ == "__main__":
    main()
