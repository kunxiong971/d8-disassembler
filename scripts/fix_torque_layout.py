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
model. We patch the GENERATED *-tq.cc / *-tq.h (rebuilt on every gn gen, located
in out.gn/x64.release/gen/torque-generated/...) to use the true C++ offsets so
both the static_asserts pass and the generated accessors read the real bytes.

Torque names the generated layout struct "<Class>Definition" (e.g.
JSInterceptorMapDefinition), so we scan that block first, then fall back to the
whole file (the matched files are dedicated to this one class layout).

This script is idempotent and safe: it only rewrites four known offset constants.

Semantics:
  - If a constant is present and at the WRONG value -> it is rewritten (fixed).
  - If a constant is present and ALREADY correct -> not rewritten (OK; e.g. an
    already-patched checkout or a version whose Torque agrees with C++).
  - If constants are NOT rewritten at all -> we dump the file content to the
    log and exit 1, so the CI log reveals the exact generated format and we can
    adjust CLASS_RE / FIX without guessing.
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

# Match a class/struct whose body holds the constants. Torque emits a trailing
# "Definition" for cpp object layout definitions; the plain name is the fallback.
CLASS_RE = re.compile(r"(?m)\b(?:class|struct)\s+JSInterceptorMap(?:Definition)?\b")

# Any line that could carry a layout/offset/constant declaration.
EVIDENCE_RE = re.compile(
    r"offset|kSize|constexpr|Interceptor|static_assert|kStart|kEnd|Layout|"
    r"k[A-Za-z_]*Offset|Definition"
)


def dump_file(path, lines):
    """Print a file's prologue + all 'evidence' lines for diagnosis."""
    name = path
    print(f"[fix_torque_layout] ============================================================")
    print(f"[fix_torque_layout] DUMP {name}  (total lines={len(lines)})")
    print(f"[fix_torque_layout] ----- prologue (first 80 lines) -----")
    for i, ln in enumerate(lines[:80], 1):
        print(f"[fix_torque_layout] {name}:{i:04d}| {ln}")
    print(f"[fix_torque_layout] ----- all evidence lines -----")
    for i, ln in enumerate(lines, 1):
        if EVIDENCE_RE.search(ln):
            print(f"[fix_torque_layout] {name}:{i:04d}| {ln}")


def find_class_block(text):
    """Return (start, end) of the JSInterceptorMap[Definition] brace block."""
    m = CLASS_RE.search(text)
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


def rewrite_constants(seg_text, label):
    """Rewrite the four known constants within a text segment.

    Handles the generated definition forms seen across .cc/.inc/.h:
      kX = 44        kX = 44 ;      kX : 44       kX{44}      kX(44)
      #define kX 44      constexpr int kX = 44
    It deliberately does NOT touch `kX == ...` (static_assert) expressions.

    Returns (new_text, fixed, present) where:
      fixed   = number of constants actually changed
      present = number of constant declarations found (any value)
    """
    new_text = seg_text
    fixed = 0
    present = 0
    for name, correct in FIX.items():
        # Match `kX` followed by an assignment-style separator then an int literal.
        pat_assign = re.compile(
            r"(\b" + re.escape(name) + r"\b\s*(?:=|[:{(])\s*)(\d+)"
        )
        # Match `#define kX 44` (whitespace separator, no operator).
        pat_define = re.compile(
            r"(#define\s+" + re.escape(name) + r"\s+)(\d+)"
        )
        pairs = [(pat_assign, 1), (pat_define, 1)]
        found = False
        for pat, grp in pairs:
            for m in pat.finditer(seg_text):
                found = True
                present += 1
                old_val = int(m.group(grp + 1))
                if old_val != correct:
                    print(
                        f"[fix_torque_layout] {label}: '{name}' = {old_val} -> {correct}"
                    )

        def make_repl(c=correct):
            def repl(m, c=c):
                nonlocal fixed
                # group(1) = `kX = ` / `kX{` / `#define kX ` ; group(2) = old int
                if int(m.group(2)) != c:
                    fixed += 1
                return m.group(1) + str(c)
            return repl

        for pat, grp in pairs:
            if pat.search(seg_text):
                new_text = pat.subn(make_repl(correct), new_text)[0]
    return new_text, fixed, present


def patch_file(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")

    span = find_class_block(text)
    if span:
        start, end = span
        block = text[start:end]
        new_block, fixed, present = rewrite_constants(block, path.name)
        if fixed:
            text = text[:start] + new_block + text[end:]
            path.write_text(text, encoding="utf-8")
            print(f"[fix_torque_layout] OK patched {path} (block, {fixed} constant(s))")
            return (True, fixed, present)
        print(f"[fix_torque_layout] block found but 0 constants fixed in {path}; dumping")
        dump_file(path, lines)
        return (False, fixed, present)

    # No block matched: fall back to whole-file rewrite.
    new_text, fixed, present = rewrite_constants(text, path.name)
    if fixed:
        path.write_text(new_text, encoding="utf-8")
        print(f"[fix_torque_layout] OK patched {path} (whole-file, {fixed} constant(s))")
        return (True, fixed, present)

    print(f"[fix_torque_layout] no block and 0 constants fixed in {path}; dumping")
    dump_file(path, lines)
    return (False, fixed, present)


def main():
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    targets = list(base.glob("gen/torque-generated/src/objects/js-interceptor-map-tq*"))
    if not targets:
        targets = list(base.glob("**/js-interceptor-map-tq.*")) or list(
            base.glob("**/js-interceptor-map-tq.cc")
        )
    if not targets:
        # No generated file: this V8 version doesn't use this layout at all.
        print("[fix_torque_layout] no js-interceptor-map-tq generated files; skipping (not applicable)")
        return 0

    total_fixed = 0
    total_present = 0
    for t in targets:
        found, fixed, present = patch_file(t)
        total_fixed += fixed
        total_present += present

    if total_fixed:
        print(f"[fix_torque_layout] done, {total_fixed} constant(s) fixed across {len(targets)} file(s)")
        return 0

    # Nothing was rewritten.
    if total_present:
        # Constants exist and are already at the right value (e.g. an already
        # patched checkout, or a V8 version whose Torque matches C++). Nothing
        # to do.
        print(f"[fix_torque_layout] constants already correct across {len(targets)} file(s); nothing to fix")
        return 0

    # Constants not fixed at all: could not adapt to the real generated format.
    # Fail loudly; the dump above reveals the actual constants for us to map.
    print("[fix_torque_layout] ERROR: js-interceptor-map-tq generated, but no JSInterceptorMap layout constants found/rewritten")
    print("[fix_torque_layout]       See dumps above; extend CLASS_RE / FIX with the real names/values.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
