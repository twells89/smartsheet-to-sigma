"""Discover unittest cases and run one deterministic shard."""
import argparse
import sys
import unittest


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("start_dir")
    parser.add_argument("shard", help="INDEX/TOTAL, 1-based")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    index, total = (int(part) for part in args.shard.split("/"))
    if not 1 <= index <= total:
        parser.error("shard must satisfy 1 <= INDEX <= TOTAL")
    tests = sorted(
        flatten(unittest.defaultTestLoader.discover(args.start_dir)),
        key=lambda test: test.id(),
    )
    selected = [
        test for position, test in enumerate(tests) if position % total == index - 1
    ]
    if args.list:
        print("\n".join(test.id() for test in selected))
        return 0
    print(f"shard {index}/{total}: {len(selected)} of {len(tests)} tests", flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(selected))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
