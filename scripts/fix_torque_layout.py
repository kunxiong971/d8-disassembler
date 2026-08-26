#!/usr/bin/env python3
"""
Scenario-specific build guard for V8 15.0.245.28.

Root cause: ExtendedMap ends at sizeof == 41 (kTaggedSize + handling), but the
Torque generator places JSInterceptorMap's own fields at
kStartOfStrongExtendedFieldsOffset = RoundUp(41, kTaggedSize) = 44, while the
C++ compiler places extended_padding_ at sizeof(ExtendedMap) == 41 and then
aligns the TaggedMembers to 4. This produces a constant +3 discrepancy:

        generated(Torque)    C++ offsetof
  extended_padding_       44              41
  named_interceptor_      47              44
  indexed_interceptor_    51              48
  kSize                   55              52

The .tq and .h sources are correct/consistent; the bug is in Torque's layout
model. We patch the GENERATED *-tq.cc (which is rebuilt on every gn gen, and is
in out.gn/x64.release/gen/torque-generated/...) to use the true C++ offsets so
both the static_asserts pass and the generated accessors read the real bytes.

This script is idempotent and safe: it only touches the JSInterceptorMap class
block, scoped by brace matching, and only rewrites the four known constants.
"""

import re
import sys
from pathlib import Path


# Mapping constant-name -> correct value (from C++ offsetof/sizeof).
FIX = {
    "kExtendedPaddingOffset": 41,
    "kNamedInterceptorOffset": 44,
    "kIndexedInterceptorOffset": 48,
    "kSize": 52,
}


def find_class_block(text, class_name):
    """Return (start_idx, end_idx) of the 'class <name>' body, brace-matched."""
    m = re.search(r"(?m)\b(?:class|struct)\s+" + re.escape(class_name) + r"\b", text)
    if not m:
        return None
    brace = text.find("{", m.end())
    if brace == -1:
        return None
    depth = 0
    i = brace
    while i < len(text):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return (m.start(), i + 1)
        i += 1
    return None


def patch_file(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    span = find_class_block(text, "JSInterceptorMap")
    if not span:
        # This V8 version doesn't use this struct layout; not applicable.
        return (False, 0)
    start, end = span
    block = text[start:end]
    new_block = block
    changed = 0
    for name, correct in FIX.items():
        # Match e.g. `kExtendedPaddingOffset = 44` / `: 44,` / `= 44`.
        pat = re.compile(r"(\b" + re.escape(name) + r"\b\s*(?:=|:)\s*)\d+")
        new_block, n = pat.subn(lambda m: m.group(1) + str(correct), new_block)
        changed += n
        print(f"[fix_torque_layout]   {name}: replaced {n} -> {correct}")
    if changed:
        new_text = text[:start] + new_block + text[end:]
        path.write_text(new_text, encoding="utf-8")
        print(f"[fix_torque_layout] OK patched {path} ({changed} constant(s))")
    else:
        # Found the class but couldn't rewrite its constants: unexpected for a
        # version that uses this layout.
        print(f"[fix_torque_layout] FOUND JSInterceptorMap but 0 changes in {path}")
    return (True, changed)


def main():
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    targets = list(base.glob("gen/torque-generated/src/objects/js-interceptor-map-tq*"))
    if not targets:
        # Fall back to a wider search under the build dir.
        targets = list(base.glob("**/js-interceptor-map-tq.*")) or list(
            base.glob("**/js-interceptor-map-tq.cc")
        )
    if not targets:
        # No generated file: this V8 version doesn't use this layout at all.
        print("[fix_torque_layout] no js-interceptor-map-tq generated files; skipping (not applicable)")
        return 0

    total = 0
    saw_class = False
    for t in targets:
        found, n = patch_file(t)
        saw_class = saw_class or found
        total += n

    print(f"[fix_torque_layout] done, {total} constant(s) fixed across {len(targets)} file(s)")
    if saw_class and total == 0:
        # The layout exists here but we couldn't rewrite it -> likely a format
        # change; fail loudly so we refine the regex rather than build silently.
        print("[fix_torque_layout] ERROR: JSInterceptorMap present but no constants rewritten")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
