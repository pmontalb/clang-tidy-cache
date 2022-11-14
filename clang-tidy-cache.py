import os
import subprocess
import sys
import json
from blake3 import blake3
from datetime import datetime
import gzip

LOG_LEVEL = -1


def log(level, s):
    if level > LOG_LEVEL:
        return
    print("[{}] {}".format(datetime.now(), s))


class Hash:
    def __init__(self):
        self.__hasher = blake3()

    def append(self, obj):
        self.__hasher.update(obj.encode('utf-8'))

    def get_hash(self):
        h = self.__hasher.hexdigest()
        log(3, "hash[{}]".format(h))
        return h


class Cache:
    def __init__(self, args):
        if "CTC_CACHE_DIR" in os.environ:
            self.__cache_dir = os.environ["CTC_CACHE_DIR"]
        else:
            self.__cache_dir = "/tmp/ctc"
        if "CTC_REMOTE_CACHE_DIR" in os.environ:
            self.__remote_cache_dir = os.environ["CTC_REMOTE_CACHE_DIR"]
        else:
            self.__remote_cache_dir = self.__cache_dir
        log(5, "cache dir: local[{}] remote[{}]".format(self.__cache_dir, self.__remote_cache_dir))

        log(9, "processing args({})".format(args))
        self.__args = args
        self.__compiler_args = None
        self.__clang_tidy_args = None
        self.__source_file = None
        self.__fixes_file = None
        self.__parse_compiler_args(args)

    def __parse_compiler_args(self, args):
        for i in range(1, len(args)):
            if args[i] == "--":
                self.__clang_tidy_args = args[:i]
                self.__parse_export_fixes(i)
                self.__compiler_args = args[i + 1:]
                log(5,
                    "found compiler args inline: ct[{}] clang[{}] exportFile[{}]".format(self.__clang_tidy_args,
                                                                                         self.__compiler_args,
                                                                                         self.__fixes_file))
                return

            if args[i] == "-p":
                self.__clang_tidy_args = args[:i]
                self.__parse_source_file(i)
                self.__parse_export_fixes(i)
                self.__parse_compilation_database(args[i + 1])
                log(5, "compilation database found: ct({}) clang({}) exportFile[{}]".format(self.__clang_tidy_args,
                                                                                            self.__compiler_args,
                                                                                            self.__fixes_file))
                return

            if "-p=" in args[i]:
                self.__clang_tidy_args = args[:i]
                self.__parse_source_file(i)
                self.__parse_export_fixes(i)
                self.__parse_compilation_database(args[i].split("=")[1])
                log(5, "compilation database found: ct[{}] clang[{}] exportFile[{}]".format(self.__clang_tidy_args,
                                                                                            self.__compiler_args,
                                                                                            self.__fixes_file))
                return

    def __parse_source_file(self, first_index):
        for j in range(first_index, len(self.__args)):
            if self.__args[j].startswith("-"):
                j += 1
                continue
            self.__source_file = self.__args[j]
            break
        if self.__source_file is None:
            log(-1, "[warn] no source file has been provided")
            sys.exit(1)
        log(7, "found source file[{}]".format(self.__source_file))

    def __parse_export_fixes(self, last_index):
        for i in range(last_index):
            if self.__clang_tidy_args[i] == "-export-fixes":
                self.__fixes_file = self.__clang_tidy_args[i + 1]
                return
            if self.__clang_tidy_args[i] == "-export-fixes=":
                self.__fixes_file = self.__clang_tidy_args[i].split("=")[1]
                return

    def __parse_compilation_database(self, path):
        if path.endswith(","):
            path = path[:-1]
        path += "/compile_commands.json"

        with open(path, "r") as f:
            database = json.load(f)
            for obj in database:
                if obj["file"] == self.__source_file:
                    self.__compiler_args = obj["command"]
                    return
        if self.__compiler_args is None:
            log(-1, "[warn] no compiler args have been provided for src[{}]".format(self.__source_file))
            sys.exit(1)

    def __get_path(self, cache_dir, h):
        return "{}/{}/{}".format(cache_dir, h[:2], h[2:])

    def __is_cached(self, path, is_local_storage):
        # miss
        if not os.path.isdir(path):
            log(3, "miss <- path[{}] doesn't exist".format(path))
            return False

        stdout_file = "{}/stdout".format(path)
        if not os.path.isfile(stdout_file):
            log(3, "miss <- stdout file[{}] doesn't exist, trying zipped file".format(stdout_file))
            stdout_file += ".gz"
            if not os.path.isfile(stdout_file):
                return False

        if not is_local_storage:
            source = path
            dest = path.replace(self.__remote_cache_dir, self.__cache_dir)
            dest += "/../"
            log(5, "secondary storage hit: copying {} -> {}".format(source, dest))
            os.system("mkdir -p {}".format(dest))
            os.system("cp -r {} {}".format(source, dest))
        self.__on_hit(path, stdout_file, is_local_storage)
        return True

    def __get_preprocessor_args(self):
        preprocess_compiler_args = []
        tokens = self.__compiler_args.split(" ")

        skip_next = False
        for i in range(len(tokens)):
            if skip_next:
                skip_next = False
                continue

            if tokens[i] == "-c":
                preprocess_compiler_args += ["-E"]
            elif tokens[i] in ["-o", "--output"]:
                skip_next = True
            else:
                preprocess_compiler_args += [tokens[i]]
        return preprocess_compiler_args

    def __preprocess_source_file(self):
        preprocess_compiler_args = self.__get_preprocessor_args()

        log(5, "running the preprocessor[{}]".format(" ".join(preprocess_compiler_args)))
        ret = subprocess.run(preprocess_compiler_args, universal_newlines=True,
                             stdout=subprocess.PIPE, check=True)
        return ret.stdout

    def __get_hash(self):
        preprocessed_file = self.__preprocess_source_file()

        hasher = Hash()
        hasher.append(preprocessed_file)
        hasher.append(self.__compiler_args)

        clang_tidy_hashable_args = []
        i = 0
        while i < len(self.__clang_tidy_args):
            if "-export-fixes" in self.__clang_tidy_args[i]:
                i += 2
                continue
            if "/tmp" in self.__clang_tidy_args[i]:
                raise ValueError(self.__clang_tidy_args[i])
            clang_tidy_hashable_args.append(self.__clang_tidy_args[i])
            i += 1
        hasher.append(" ".join(clang_tidy_hashable_args))
        return hasher.get_hash()

    def __store_stdout(self, path, h, stdout):
        stdout_file = "{}/stdout".format(path)

        compress = "CTC_COMPRESS" in os.environ or "CTC_COMPRESS_STDOUT" in os.environ
        if compress:
            stdout_file += ".gz"

        log(6, "file[{}] -> hash[{}]: storing stdout to [{}]".format(self.__source_file, h, stdout_file))
        assert ("CTC_FORCE" in os.environ or not os.path.isfile(stdout_file))

        if compress:
            with gzip.open("{}/stdout.gz".format(path), "wb") as f:
                f.write(stdout.encode('utf-8'))
        else:
            with open("{}/stdout".format(path), "w") as f:
                f.write(stdout)

    def __store_fixes_file(self, path, h):
        if self.__fixes_file is not None:
            assert (os.path.isfile(self.__fixes_file))
            cached_fixes_file = "{}/fixes.yaml".format(path)
            compress = "CTC_COMPRESS" in os.environ or "CTC_COMPRESS_FIXES" in os.environ

            log(6, "file[{}] -> hash[{}]: storing fixes file[{}]".format(self.__source_file, h, cached_fixes_file))
            os.system("cp {} {}".format(self.__fixes_file, cached_fixes_file))
            if compress:
                log(9, "file[{}] -> hash[{}]: compressing fixes file[{}]".format(self.__source_file, h, cached_fixes_file))
                os.system("gzip {}".format(cached_fixes_file))

    def __on_hit(self, path, stdout_file, is_local_storage):
        log(7, "hit <- printing the stdout from[{}]".format(stdout_file))
        # hit: print the stdout file to stdout
        if stdout_file.endswith(".gz"):
            if "CTC_DO_NOT_PRINT_STDOUT" not in os.environ:
                with gzip.open(stdout_file, "rt") as lines:
                    for line in lines:
                        print(line)
        else:
            if "CTC_DO_NOT_PRINT_STDOUT" not in os.environ:
                with open(stdout_file, "r") as f:
                    lines = f.readlines()
                    for line in lines:
                        print(line)
            if "CTC_COMPRESS" in os.environ or "CTC_COMPRESS_STDOUT" in os.environ:
                # compress it so that next time we'll read off the compressed file
                log(9, "hit <- compressing[{}]".format(stdout_file))
                os.system("gzip -f {}".format(stdout_file))

        # additionally, if requested, save the fixes file where clang-tidy expects it
        if self.__fixes_file is not None:
            os.system("rm -f {}".format(self.__fixes_file))
            cached_fixes_file = "{}/fixes.yaml".format(path)
            if not os.path.isfile(cached_fixes_file):
                cached_fixes_file += ".gz"
                if not os.path.isfile(cached_fixes_file):
                    log(-1, "[error] couldn't find fixes file[{}]".format(cached_fixes_file))
                    return
            log(8, "hit <- copying [{}] to [{}]".format(cached_fixes_file, self.__fixes_file))
            if cached_fixes_file.endswith(".gz"):
                os.system("cp {} {}.gz".format(cached_fixes_file, self.__fixes_file))
                log(9, "unzipping fixes file[{}.gz]".format(self.__fixes_file))
                os.system("gunzip {}.gz".format(self.__fixes_file))
                assert (not os.path.isfile("{}.gz".format(self.__fixes_file)))
                assert (os.path.isfile("{}".format(self.__fixes_file)))
            else:
                os.system("cp {} {}".format(cached_fixes_file, self.__fixes_file))
                if "CTC_COMPRESS" in os.environ or "CTC_COMPRESS_FIXES" in os.environ:
                    # compress it so that next time we'll read off the compressed file
                    log(9, "hit <- compressing[{}]".format(stdout_file))
                    os.system("gzip {}".format(cached_fixes_file))

        self.__update_cache_stats(hit=True, is_local_storage=is_local_storage)
        return True

    def __on_miss(self, path, h):
        log(2, "file[{}] -> hash[{}]: miss".format(self.__source_file, h))

        # miss: actually run clang-tidy
        log(5, "running clang-tidy[{}]".format(self.__args))

        try:
            ret = subprocess.run(self.__args, universal_newlines=True, stdout=subprocess.PIPE, check=True)
        except subprocess.CalledProcessError as err:
            log(-1, "[critical] clang-tidy couldn't compile[out={} | err={}]".format(err.stdout, err.stderr))
            sys.exit(1)
        log(9, "clang-tidy output[{}]".format(ret.stdout))

        if not os.path.isdir(path):
            os.system("mkdir -p {}".format(path))

        # store stdout
        self.__store_stdout(path, h, ret.stdout)

        # store fixes file
        self.__store_fixes_file(path, h)

        self.__update_cache_stats(hit=False, is_local_storage=True)

    def __update_cache_stats(self, hit, is_local_storage):
        stats_file = "{}/stats.json".format(self.__cache_dir)
        if os.path.isfile(stats_file):
            with open(stats_file, "r") as f:
                j = json.load(f)
                if is_local_storage:
                    _j = j["local"]
                else:
                    _j = j["remote"]
                if hit:
                    _j["hit"] += 1
                else:
                    _j["miss"] += 1
        else:
            with open(stats_file, "w") as f:
                j = {"local": {"hit": 0, "miss": 0},
                     "remote": {"hit": 0, "miss": 0}, }
                if is_local_storage:
                    _j = j["local"]
                else:
                    _j = j["remote"]
                if hit:
                    _j["hit"] += 1
                else:
                    _j["miss"] += 1
        with open(stats_file, "w") as f:
            json.dump(j, f)

    def run(self):
        if self.__compiler_args is None:
            log(-1, "[warn] no compiler args have been found")
            return

        assert (len(self.__compiler_args) > 0)
        assert (len(self.__clang_tidy_args) > 0)

        h = self.__get_hash()
        log(6, "file[{}] -> hash[{}]".format(self.__source_file, h))

        path = self.__get_path(self.__cache_dir, h)
        if "CTC_FORCE" not in os.environ:
            # check local storage
            if self.__is_cached(path, is_local_storage=True):
                log(2, "file[{}] -> hash[{}]: local hit!".format(self.__source_file, h))
                # hit: nothing to do
                return

            # check remote storage
            if self.__remote_cache_dir != self.__cache_dir:
                remote_path = self.__get_path(self.__remote_cache_dir, h)
                if self.__is_cached(remote_path, is_local_storage=False):
                    log(2, "file[{}] -> hash[{}]: remote hit!".format(self.__source_file, h))
                    # hit: nothing to do
                    return

        self.__on_miss(path, h)

    @staticmethod
    def show_stats():
        if "CTC_CACHE_DIR" in os.environ:
            cache_dir = os.environ["CTC_CACHE_DIR"]
        else:
            cache_dir = "/tmp/ctc"
        if "CTC_REMOTE_CACHE_DIR" in os.environ:
            remote_cache_dir = os.environ["CTC_REMOTE_CACHE_DIR"]
        else:
            remote_cache_dir = cache_dir

        stats_file = "{}/stats.json".format(cache_dir)
        if not os.path.isfile(stats_file):
            print("*** No stats have been produced ***")
            return

        print("Stats File: {}".format(stats_file))
        with open(stats_file, "r") as f:
            j = json.load(f)
            local_hits = int(j["local"]["hit"])
            local_miss = int(j["local"]["miss"])
            local_total = local_hits + local_miss
            local_hit_pct = 100.0 * local_hits / (local_total + 1e-16)
            local_miss_pct = 0 if local_total == 0 else 100.0 - local_hit_pct
            print("* Local Storage")
            print("*** Hits: {:5.2f}% ({} / {})".format(local_hit_pct, local_hits, local_total))
            print("*** Miss: {:5.2f}% ({} / {})".format(local_miss_pct, local_miss, local_total))
            print("*** Size: {}".format(subprocess.check_output(['du', '-sh', cache_dir]).split()[0].decode('utf-8')))

            if remote_cache_dir != cache_dir:
                remote_hits = int(j["remote"]["hit"])
                remote_miss = int(j["remote"]["miss"])
                remote_total = remote_hits + remote_miss
                remote_hit_pct = 100.0 * remote_hits / (remote_total + 1e-16)
                remote_miss_pct = 0 if remote_total == 0 else 100.0 - remote_hit_pct
                print("* Remote Storage")
                print("*** Hits: {:5.2f}% ({} / {})".format(remote_hit_pct, remote_hits, remote_total))
                print("*** Miss: {:5.2f}% ({} / {})".format(remote_miss_pct, remote_miss, remote_total))
                print("*** Size: {}".format(subprocess.check_output(['du', '-sh', remote_cache_dir]).split()[0].decode('utf-8')))

    @staticmethod
    def reset_stats():
        if "CTC_CACHE_DIR" in os.environ:
            cache_dir = os.environ["CTC_CACHE_DIR"]
        else:
            cache_dir = "/tmp/ctc"

        stats_file = "{}/stats.json".format(cache_dir)
        if not os.path.isfile(stats_file):
            print("*** No stats have been produced ***")
            return
        with open(stats_file, "w") as f:
            j = {"local" : {"hit": 0, "miss": 0},
                 "remote": {"hit": 0, "miss": 0}, }
        with open(stats_file, "w") as f:
            json.dump(j, f)
        print("*** Stats reset done ***")

    @staticmethod
    def reset_cache():
        if "CTC_CACHE_DIR" in os.environ:
            cache_dir = os.environ["CTC_CACHE_DIR"]
        else:
            cache_dir = "/tmp/ctc"
        os.system("rm -r {}".format(cache_dir))
        print("*** Cleared cache ***")


if __name__ == "__main__":
    if "CTC_DEBUG" in os.environ:
        LOG_LEVEL = int(os.environ["CTC_DEBUG"])

    if "--show-stats" in sys.argv:
        Cache.show_stats()
        sys.exit(0)
    if "--clear-stats" in sys.argv or "--reset-stats" in sys.argv:
        Cache.reset_stats()
        sys.exit(0)
    if "--clear-cache" in sys.argv or "--reset-cache" in sys.argv:
        Cache.reset_cache()
        sys.exit(0)

    cache = Cache(sys.argv[1:])
    cache.run()
    sys.exit(0)