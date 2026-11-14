#!/usr/bin/env python3
"""Compare two JCAlgTest ALGSUPPORT csv/log files.

Format-agnostic: JCAlgTest 1.8.4 writes ';' separated rows, older versions
write ',' separated rows. Names and values never contain either character,
so splitting on the first separator of either kind handles both.

Usage: python3 jcalgtest_compare.py A.csv B.csv [--perf]
"""

import re
import sys

SEP = re.compile(r"[;,]")
SKIP_PREFIX = ("INFO:", "Tested", "AlgTest", "Used reader")


def load(path):
    data = {}
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line:
                continue
            parts = SEP.split(line)
            if len(parts) < 2:
                continue
            name = parts[0].strip()
            if not name or name.startswith(SKIP_PREFIX):
                continue
            val = parts[1].strip()
            perf = None
            for p in parts[2:]:
                p = p.strip()
                if p:
                    try:
                        perf = float(p)
                        break
                    except ValueError:
                        continue
            data[name] = (val, perf)
    return data


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    a = load(sys.argv[1])
    b = load(sys.argv[2])
    with_perf = "--perf" in sys.argv

    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))
    common = sorted(set(a) & set(b))
    diff = [k for k in common if a[k][0] != b[k][0]]

    print("A: %s (%d entries)" % (sys.argv[1], len(a)))
    print("B: %s (%d entries)" % (sys.argv[2], len(b)))
    print(
        "common: %d | identical: %d | differing: %d"
        % (len(common), len(common) - len(diff), len(diff))
    )
    if common:
        print("similarity: %.2f%%" % (100.0 * (len(common) - len(diff)) / len(common)))

    if diff:
        print("\n== support differs (A -> B)")
        for k in diff:
            print("  %-55s %-12s -> %s" % (k[:55], a[k][0], b[k][0]))

    if only_a:
        print("\n== only in A (%d)" % len(only_a))
        for k in only_a[:20]:
            print("  " + k[:70])
    if only_b:
        print("\n== only in B (%d)" % len(only_b))
        for k in only_b[:20]:
            print("  " + k[:70])

    if with_perf:
        print("\n== timing differs >20%")
        n = 0
        for k in common:
            pa, pb = a[k][1], b[k][1]
            if pa and pb and pa > 0 and pb > 0:
                r = max(pa, pb) / min(pa, pb)
                if r > 1.2:
                    print("  %-50s %.4f -> %.4f (x%.1f)" % (k[:50], pa, pb, r))
                    n += 1
                    if n >= 30:
                        break
        if n == 0:
            print("  (none)")


if __name__ == "__main__":
    main()
