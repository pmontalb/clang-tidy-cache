#!/usr/bin/python3

import os
import sys
import shlex

DEFAULT_CLANG_TIDY = "/usr/bin/clang-tidy"
DEFAULT_CLANG_TIDY_CACHE = "~/bin/clang-tidy-cache"


def main():
    args = " ".join(map(shlex.quote, sys.argv[1:]))

    clang_tidy_bin = DEFAULT_CLANG_TIDY
    if "CTC_CLANG_TIDY" in os.environ:
        clang_tidy_bin = os.environ["CTC_CLANG_TIDY"]

    if "CTC_DISABLE" in os.environ or "-list-checks" in sys.argv:
        return os.system("{} {}".format(clang_tidy_bin, args))

    clang_tidy_cache_bin = "{}/clang-tidy-cache.py".format(os.getcwd())
    if "CTC_CLANG_TIDY_CACHE" in os.environ:
        clang_tidy_cache_bin = os.environ["CTC_CLANG_TIDY_CACHE"]

    return os.system("python {} {} {}".format(clang_tidy_cache_bin, clang_tidy_bin, args))


if __name__ == "__main__":
    sys.exit(main())