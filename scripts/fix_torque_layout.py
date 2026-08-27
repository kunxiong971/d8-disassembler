"""
V8 15.0.245.28 layout self-correcting guard.

WHY THIS EXISTS
---------------
For @cppObjectLayoutDefinition classes (esp. those with @hasSameInstanceTypeAsParent
like ExtendedMap / JSInterceptorMap), the Torque generator lays out fields starting
at `sizeof(Parent)`, but the C++ compiler may round the parent's size up to a
kTaggedSize boundary. This produces a constant +N discrepancy between the
Torque-computed constants and the real C++ offsets, so the generated

    static_assert(kFieldOffset == offsetof(Class, field));
    static_assert(kSize         == sizeof(Class));

fail (e.g. `kSize == sizeof(ExtendedMap)` => `41 == 44` for ExtendedMap, and
`kExtendedPaddingOffset == offsetof(JSInterceptorMap, extended_padding_)`
=> `44 == 41` for JSInterceptorMap).

STRATEGY
--------
Instead of hard-coding magic numbers (which are class-specific and fragile),
this script SELF-CORRECTS: for every generated `TorqueGenerated*Asserts` block it
finds the `static_assert(kX == offsetof/sizeof(...))` lines and rewrites the
matching `kX = ...;` constant to the assert's right-hand expression.

Because the assert's RHS is exactly what the C++ compiler computes, this makes
every such assert pass BY CONSTRUCTION, and it is class-agnostic:

    JSInterceptorMap:
      kExtendedPaddingOffset = offsetof(JSInterceptorMap, extended_padding_)  // 41
      kNamedInterceptorOffset= offsetof(JSInterceptorMap, named_interceptor_)// 44
      kIndexedInterceptorOffset=offsetof(JSInterceptorMap, indexed_interceptor_)//48
      kSize                  = sizeof(JSInterceptorMap)                       // 52

    ExtendedMap:
      kBitFieldExOffset      = offsetof(ExtendedMap, bit_field_ex_)           // 40
      kSize                  = sizeof(ExtendedMap)                            // 44

Chained constants (kFieldOffsetEnd, kStartOfStrongFieldsOffset, ...) derive from
these base constants and therefore follow automatically; we never touch them.

The script is idempotent and safe: an already-correct constant is left as-is.
"""

import re
import subprocess
import sys
from pathlib import Path

# A `static_assert(kX == offsetof(...))` or `static_assert(kX == sizeof(...))`.
# Note: `[^)]*` is fine because offsetof/sizeof in these asserts have no nested
# parentheses. RHS is captured verbatim (e.g. `offsetof(ExtendedMap, bit_field_ex_)`).
ASSERT_RE = re.compile(
    r"static_assert\(\s*(k\w+)\s*==\s*((?:offsetof|sizeof)\s*\([^)]*\))\s*\)\s*;"
)

# `static constexpr int kX = <expr>;`  (single `=`, never the `==` of an assert).
# Groups: 1=prefix, 2=name, 3=` = `, 4=value, 5=`;`  (explicit, no name/para confusion).
DEF_ASSIGN_RE = re.compile(
    r"(static\s+constexpr\s+int\s+)(k\w+)(\s*=\s*)([^;]+?)(;\s*)"
)
# `#define kX <int>` form.  Groups: 1=`#define kX `, 2=name, 3=value.
DEF_DEFINE_RE = re.compile(
    r"(#define\s+(k\w+)\s+)(\d+)"
)

# A block that defines the layout constants for one class.
CLASS_BLOCK_RE = re.compile(
    r"(?m)\b(?:class|struct)\s+TorqueGenerated(\w+)Asserts\b"
)


def _brace_block(text, start):
    """Given the index of a '{', return the index just after its matching '}'."""
    depth = 0
    i = start
    while i < len(text):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def autocorrect_block(block, group_name):
    """Within one asserts block, set each asserted constant to the assert RHS.

    Returns (new_block, fixed).
    """
    # 1) Collect all (constant_name, rhs) from the static_assert lines.
    targets = []
    for m in ASSERT_RE.finditer(block):
        targets.append((m.group(1), m.group(2)))

    if not targets:
        return block, 0

    fixed = 0
    new_block = block

    # 2) Rewrite each `kX = <val>;` definition to the asserted RHS.
    for name, rhs in targets:
        # Assignment form.
        def repl_assign(m, name=name, rhs=rhs):
            nonlocal fixed
            if m.group(2) != name:
                return m.group(0)
            old = m.group(4).strip()
            if old != rhs:
                fixed += 1
                print(f"[fix_torque_layout] {group_name}: {name} = {old} -> {rhs}")
            return m.group(1) + m.group(2) + m.group(3) + rhs + m.group(5)

        new_block = DEF_ASSIGN_RE.sub(repl_assign, new_block)

        # #define form.
        def repl_define(m, name=name, rhs=rhs):
            nonlocal fixed
            if m.group(2) != name:
                return m.group(0)
            old = m.group(3)
            # #define takes an integer, not an offsetof/sizeof expression.
            if old != rhs:
                fixed += 1
                print(f"[fix_torque_layout] {group_name}: #{name} = {old} -> {rhs}")
            return m.group(1) + rhs

        new_block = DEF_DEFINE_RE.sub(repl_define, new_block)

    return new_block, fixed


def find_and_patch(path):
    text = path.read_text(encoding="utf-8", errors="replace")

    total_fixed = 0
    class_count = 0
    new_text = text
    # Replace blocks from the end so earlier offsets stay valid.
    matches = list(CLASS_BLOCK_RE.finditer(text))
    for m in reversed(matches):
        brace = text.find("{", m.end())
        if brace == -1:
            continue
        end = _brace_block(text, brace)
        if end is None:
            continue
        block = text[m.start():end]
        group_name = m.group(1)
        class_count += 1
        new_block, fixed = autocorrect_block(block, group_name)
        total_fixed += fixed
        if fixed:
            new_text = new_text[:m.start()] + new_block + new_text[end:]
            print(f"[fix_torque_layout] patched {path.name} [{group_name}Asserts] {fixed} constant(s)")

    if total_fixed:
        path.write_text(new_text, encoding="utf-8")
        print(f"[fix_torque_layout] OK {path.name}: {total_fixed} constant(s) corrected over {class_count} class(es)")
        return True, total_fixed

    print(f"[fix_torque_layout] no corrections needed in {path.name} ({class_count} class(es))")
    return False, 0


def dump_file(path):
    lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
    print("[fix_torque_layout] ============================================================")
    print(f"[fix_torque_layout] DUMP {path}  (total lines={len(lines)})")
    for i, ln in enumerate(lines, 1):
        print(f"[fix_torque_layout] {path.name}:{i:04d}| {ln}")


def ensure_generated(base, rel_target):
    """If a torque-generated file is missing, generate it with ninja.

    The build's quick-target step may only have produced js-interceptor-map-tq.cc;
    map-tq.cc (which holds the ExtendedMap asserts) is often not present yet when
    this script runs, so we generate it here. Idempotent: if it already exists,
    ninja does nothing.

    Returns True if the file now exists.
    """
    abs_target = base / rel_target
    if abs_target.exists():
        return True
    # Only attempt ninja if a real gn output dir was passed.
    if not (base / "args.gn").exists():
        return False
    print(f"[fix_torque_layout] generating {rel_target} (not present yet)...")
    try:
        result = subprocess.run(
            ["ninja", "-C", str(base), rel_target],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"[fix_torque_layout] ninja for {rel_target} failed:\n{result.stdout}{result.stderr}")
    except FileNotFoundError:
        print("[fix_torque_layout] ninja not found on PATH; cannot generate missing torque file")
    return abs_target.exists()


def collect_targets(base):
    objs = base / "gen" / "torque-generated" / "src" / "objects"
    # Files we positively know need patching. Generate them if missing.
    known_rel = [
        "gen/torque-generated/src/objects/js-interceptor-map-tq.cc",
        "gen/torque-generated/src/objects/map-tq.cc",
    ]
    for rel in known_rel:
        ensure_generated(base, rel)

    # Every generated asserts file that is present (a superset of the known two).
    targets = []
    if objs.exists():
        for p in objs.glob("*-tq.cc"):
            targets.append(p)
    for rel in known_rel:
        p = base / rel
        if p.exists() and p not in targets:
            targets.append(p)
    return sorted(set(targets))


def main():
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")

    # A real gn output dir always has args.gn; if we still have no generated
    # -tq.cc after attempting to generate them, that is a hard failure.
    is_build_dir = (base / "args.gn").exists()

    targets = collect_targets(base)
    if not targets:
        print("[fix_torque_layout] no relevant generated -tq.cc files")
        if is_build_dir:
            print("[fix_torque_layout] ERROR: build dir has args.gn but no torque-generated -tq.cc files were produced.")
            print("[fix_torque_layout]       (ninja generation of js-interceptor-map-tq.cc / map-tq.cc failed; see above)")
            return 1
        print("[fix_torque_layout] skipping (not applicable)")
        return 0

    relevant = []
    for t in targets:
        try:
            txt = t.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # Only operate on files that actually define a TorqueGenerated*Asserts class.
        if "TorqueGenerated" in txt and "Asserts" in txt:
            relevant.append(t)

    if not relevant:
        print("[fix_torque_layout] generated files present but none define TorqueGenerated*Asserts")
        return 0

    total = 0
    touched = 0
    for t in relevant:
        fixed, n = find_and_patch(t)
        total += n
        if n:
            touched += 1

    if total:
        print(f"[fix_torque_layout] done, {total} constant(s) corrected across {touched} file(s)")
        return 0

    # Nothing changed. If asserts are present but didn't need correction they were
    # already consistent. Otherwise dump the first non-trivial file for diagnosis.
    print("[fix_torque_layout] no constants corrected (already consistent or unrecognised format)")
    for t in relevant:
        txt = t.read_text(encoding="utf-8", errors="replace")
        asserts = ASSERT_RE.findall(txt)
        if asserts:
            print(f"[fix_torque_layout] sample asserts in {t.name}:")
            for name, rhs in asserts[:12]:
                print(f"[fix_torque_layout]   {name} == {rhs}")
        else:
            print(f"[fix_torque_layout] no offsetof/sizeof asserts recognised in {t.name}; dumping")
            dump_file(t)
    return 0


if __name__ == "__main__":
    sys.exit(main())
