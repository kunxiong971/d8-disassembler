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

We patch the GENERATED *-tq.cc (rebuilt on every gn gen, located in
out.gn/x64.release/gen/torque-generated/src/objects/...) so the static_asserts
pass.

CRITICAL (as observed in the real generated file for this V8): the constants are
NOT integer literals, they are a chained EXPRESSION beginning with
`sizeof(ExtendedMap)`:

    static constexpr int kExtendedPaddingOffset = sizeof(ExtendedMap);
    static constexpr int kExtendedPaddingOffsetEnd = kExtendedPaddingOffset + (3*kUInt8Size) - 1;
    static constexpr int kNamedInterceptorOffset   = kExtendedPaddingOffsetEnd + 1;
    static constexpr int kNamedInterceptorOffsetEnd = kNamedInterceptorOffset + kTaggedSize - 1;
    static constexpr int kIndexedInterceptorOffset = kNamedInterceptorOffsetEnd + 1;
    static constexpr int kIndexedInterceptorOffsetEnd = kIndexedInterceptorOffset + kTaggedSize - 1;
    static constexpr int kSize = kIndexedInterceptorOffsetEnd + 1;

`sizeof(ExtendedMap)` evaluates to 44 in C++ (the class is 4-byte-aligned so the
size rounds 41 -> 44), but V8's object layout places the first JSInterceptorMap
field (extended_padding_) at byte offset 41. So `kExtendedPaddingOffset` must be
hard-set to 41; the whole chain then yields 41/44/48/52 which match C++.

This script is idempotent and safe: it only rewrites the four known offset
constants, whether they are written as literals OR as expressions, and it never
touches `static_assert(kX == ...)` lines.

Semantics:
  - If a constant is present and at the WRONG value -> it is rewritten (fixed).
  - If a constant is present and ALREADY correct -> not rewritten (OK; e.g. an
    already-patched checkout or a version whose Torque agrees with C++).
  - If constants are NOT rewritten at all -> we dump the file content to the
    log and exit 1, so the CI log reveals the exact generated format.
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


# Match a class/struct whose body holds the constants (several naming variants).
CLASS_RE = re.compile(
    r"(?m)\b(?:class|struct)\s+TorqueGeneratedJSInterceptorMap(?:Asserts|Definition)?\b"
)

# Any line that could carry a layout/offset/constant declaration.
EVIDENCE_RE = re.compile(
    r"offset|kSize|constexpr|Interceptor|static_assert|kStart|kEnd|Layout|"
    r"k[A-Za-z_]*Offset|Definition"
)


def dump_file(path, lines):
    """Print a file's prologue + all 'evidence' lines for diagnosis."""
    name = path
    print("[fix_torque_layout] ============================================================")
    print(f"[fix_torque_layout] DUMP {name}  (total lines={len(lines)})")
    print("[fix_torque_layout] ----- prologue (first 80 lines) -----")
    for i, ln in enumerate(lines[:80], 1):
        print(f"[fix_torque_layout] {name}:{i:04d}| {ln}")
    print("[fix_torque_layout] ----- all evidence lines -----")
    for i, ln in enumerate(lines, 1):
        if EVIDENCE_RE.search(ln):
            print(f"[fix_torque_layout] {name}:{i:04d}| {ln}")


def find_class_block(text):
    """Return (start, end) of the JSInterceptorMap* brace block."""
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

    Handles both literal (`kX = 44;`) and chained-expression
    (`kX = sizeof(ExtendedMap);`, `kX = kExtendedPaddingOffsetEnd + 1;`) forms,
    plus `#define kX 44`. It NEVER matches the `==` of a static_assert.

    Returns (new_text, fixed, present) where:
      fixed   = number of constants actually changed
      present = number of constant declarations found (any value)
    """
    new_text = seg_text
    fixed = 0
    present = 0
    for name, correct in FIX.items():
        # `NAME = <expr>;` — the `(?<![=])=(?![=])` guard matches a single `=`
        # and never the `==` of `static_assert(NAME == ...)`.
        pat_assign = re.compile(
            r"(\b" + re.escape(name) + r"\b\s*(?<![=])=(?![=])\s*)([^;]+?)(\s*;)"
        )
        # `#define NAME <int>`
        pat_define = re.compile(
            r"(#define\s+" + re.escape(name) + r"\s+)(\d+)"
        )

        m_assign = pat_assign.finditer(seg_text)
        m_define = pat_define.finditer(seg_text)
        present += sum(1 for _ in m_assign)
        present += sum(1 for _ in m_define)

        def repl_assign(m, c=correct):
            nonlocal fixed
            old = m.group(2).strip()
            if old != str(c):
                fixed += 1
                print(f"[fix_torque_layout] {label}: '{name}' = {old} -> {c}")
            return m.group(1) + str(c) + m.group(3)

        def repl_define(m, c=correct):
            nonlocal fixed
            old = m.group(2)
            if old != str(c):
                fixed += 1
                print(f"[fix_torque_layout] {label}: '{name}' = {old} -> {c}")
            return m.group(1) + str(c)

        new_text = pat_assign.subn(repl_assign, new_text)[0]
        new_text = pat_define.subn(repl_define, new_text)[0]
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
        print("[fix_torque_layout] no js-interceptor-map-tq generated files; skipping (not applicable)")
        return 0

    # Only operate on the file(s) that actually define the four layout constants.
    # This avoids dumping the (unrelated, huge) -csa.cc / -csa.h noise and avoids
    # a false 'nothing found'.
    relevant = []
    for t in targets:
        try:
            txt = t.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if any(k in txt for k in FIX):
            relevant.append(t)
        else:
            print(f"[fix_torque_layout] skipping (no layout constants): {t.name}")

    if not relevant:
        print("[fix_torque_layout] generated files present but none define the JSInterceptorMap layout constants")
        for t in targets:
            try:
                txt = t.read_text(encoding="utf-8", errors="replace")
                if len(txt.split("\n")) <= 60:
                    dump_file(t, txt.split("\n"))
            except Exception:
                pass
        return 1

    total_fixed = 0
    total_present = 0
    for t in relevant:
        found, fixed, present = patch_file(t)
        total_fixed += fixed
        total_present += present

    if total_fixed:
        print(f"[fix_torque_layout] done, {total_fixed} constant(s) fixed across {len(relevant)} file(s)")
        return 0

    if total_present:
        print(f"[fix_torque_layout] constants already correct across {len(relevant)} file(s); nothing to fix")
        return 0

    print("[fix_torque_layout] ERROR: js-interceptor-map-tq generated, but no JSInterceptorMap layout constants found/rewritten")
    print("[fix_torque_layout]       See dumps above; extend the matchers / FIX with the real names/values.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
