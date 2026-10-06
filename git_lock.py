#!/usr/bin/env python3
"""git のインデックスを触る操作を1つのロックの下で走らせる。

なぜ要るか: **インデックスはワークツリーに1つしかない共有資源**で、`git add` から
`git commit` までの間は誰でも踏める窓になっている。同じワークツリーで複数のAIエージェントや
スクリプトが動くと、実際にこう壊れる——Aが9ファイルを add した直後に、別プロセスBが
commit を打ち、Aの staged をそのままBの題で取り込む。Aの commit は「no changes added」で
空振りし、**題と中身が食い違ったコミットが1本残る**。題が似ていれば黙って混ざる。

防止は**add から commit までを1つの flock に閉じ込める**こと。

使い方(add と commit を**1つの区間**にすること。別々に2回呼ぶと窓がまた開く):

    python3 git_lock.py --sh 'git add -A src && git commit -m "..."'
    python3 git_lock.py -- git commit --amend -m "..."
    python3 git_lock.py -- status --short  # git の省略にも対応
    python3 git_lock.py --nowait --sh '...'   # 待たずに断られたい場合

- 既定は**待つ**(黙って先へ進まない)。既定の上限は600秒で、超えたら 75 で終わる。
- `--nowait` は待たずに 75 で断る。どちらの場合も、先客の pid とコマンドを stderr に出す。
- ロックは `<git-dir>/index-guard.lock` の flock。プロセスが死ねば OS が外すので、腐った錠は残らない。
- 入れ子は素通しする(環境変数 `GIT_INDEX_LOCK_HELD` が同じ錠を指していれば取り直さない)。
  自分で自分を待つ死に方を避けるため。
- 省略対応は下記の Git 操作だけに限定し、それ以外の実行ファイル・シェル行・終了コードはそのまま扱う。

検査: `--self-test` が上の事故と同じ手順(9ファイルを add した直後に別プロセスが commit)を
使い捨てのリポジトリで起こし、**ロック無しでは事故が起き、ロック有りでは起きない**ことを実測する。
CLIの回帰検査: `python3 -m unittest discover -s tests`。
"""
import argparse
import fcntl
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WAIT_DEFAULT = 600.0     # 秒。黙って先へ進むより待つ
BUSY_EXIT = 75           # EX_TEMPFAIL。「断られた」を呼び出し側が見分けられる終了コード
POLL = 0.05
ENV_KEY = "GIT_INDEX_LOCK_HELD"
LOCK_NAME = "index-guard.lock"
# `-- status` のような省略形を git の操作として扱う。
GIT_SHORTHAND = frozenset({"add", "commit", "diff", "log", "show", "status"})


def git_dir(cwd: str) -> Path:
    """このワークツリーの .git を求める。インデックスはここに1つだけ在る。"""
    out = subprocess.run(["git", "-C", cwd, "rev-parse", "--absolute-git-dir"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"git のリポジトリではない: {cwd}\n{out.stderr.strip()}")
    return Path(out.stdout.strip())


def lock_path(cwd: str) -> Path:
    return git_dir(cwd) / LOCK_NAME


def _holder(fd) -> str:
    """先客の名乗り。錠の中身は診断用で、判定には使わない(判定は flock だけ)。"""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 4096).decode("utf-8", "replace").strip() or "(名乗り無し)"
    except OSError:
        return "(読めない)"


def _sign(fd, label: str) -> None:
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    os.write(fd, f"pid={os.getpid()} since={stamp} cmd={label}\n".encode("utf-8"))
    os.fsync(fd)


def run_locked(label: str, runner, *, cwd: str, wait: float, nowait: bool,
               quiet: bool = False) -> int:
    """錠を取ってから runner() を走らせる。取れるまで待つ(または断る)。"""
    path = lock_path(cwd)
    if os.environ.get(ENV_KEY) == str(path):
        # 入れ子。すでに同じ錠の中に居るので取り直さない(取ると自分を待って死ぬ)。
        return runner()
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.time() + (0.0 if nowait else wait)
        told = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                who = _holder(fd)
                if nowait:
                    print(f"git のインデックスは先客が使用中。断る: {who}", file=sys.stderr)
                    return BUSY_EXIT
                if not told and not quiet:
                    print(f"git のインデックスは先客が使用中。待つ: {who}", file=sys.stderr)
                    told = True
                if time.time() >= deadline:
                    print(f"待ち時間 {wait:.0f}s を超えた。断る: {who}", file=sys.stderr)
                    return BUSY_EXIT
                time.sleep(POLL)
        _sign(fd, label)
        os.environ[ENV_KEY] = str(path)
        try:
            return runner()
        finally:
            os.environ.pop(ENV_KEY, None)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(add_help=True, description="git インデックスの実行ロック")
    p.add_argument("--sh", help="1区間として走らせるシェル行(add と commit をまとめて渡す)")
    p.add_argument("--wait", type=float, default=WAIT_DEFAULT, help="待ちの上限(秒)")
    p.add_argument("--nowait", action="store_true", help="待たずに断る(終了コード 75)")
    p.add_argument("-C", dest="cwd", default=".", help="作業ディレクトリ")
    p.add_argument("--self-test", "--selftest", dest="selftest", action="store_true")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    a = p.parse_args(argv)

    if a.selftest:
        return selftest()

    # ラッパー用の先頭区切りだけを外し、git の pathspec 用 `--` は保持する。
    rest = a.rest[1:] if a.rest[:1] == ["--"] else a.rest
    if a.sh and rest:
        print("--sh と -- の同時指定はできない", file=sys.stderr)
        return 2
    if not a.sh and not rest:
        p.print_usage(sys.stderr)
        return 2

    if a.sh:
        cmd, label = ["sh", "-c", a.sh], a.sh
    else:
        cmd = ["git", *rest] if rest[0] in GIT_SHORTHAND else rest
        label = " ".join(shlex.quote(x) for x in cmd)

    def runner() -> int:
        try:
            return subprocess.run(cmd, cwd=a.cwd).returncode
        except FileNotFoundError:
            print(f"実行ファイルまたは作業ディレクトリが見つからない: {cmd[0]!r} / {a.cwd!r}",
                  file=sys.stderr)
            return 127
        except PermissionError:
            print(f"実行権限がない: {cmd[0]!r} / {a.cwd!r}", file=sys.stderr)
            return 126

    return run_locked(label, runner, cwd=a.cwd, wait=a.wait, nowait=a.nowait)


# ---------------------------------------------------------------- 検査(事故の再注入)
#
# 本物のリポジトリでは撃たない。使い捨てのリポジトリを一時ディレクトリに作り、そこで同じ手順を
# 起こす。測るのは「題と中身が食い違ったコミットが残るか」の1点。

NINE = [f"assets/f{i}.md" for i in range(1, 10)]   # Aの9ファイル
OTHER = "notes/NOTES.md"                           # Bの1ファイル
SUBJ_A = "feat(assets): 9ファイル"
SUBJ_B = "docs(notes): NOTES"

_CHILD = r"""
import json, os, subprocess, sys, time
repo, role, start_at, gap, mode, tool = sys.argv[1:7]
start_at, gap = float(start_at), float(gap)
files = json.loads(sys.argv[7])
subject = sys.argv[8]
for f in files:
    p = os.path.join(repo, f)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(role + " の中身\n")
# 全員を同じ壁時計時刻で発進させる(起動のばらつきで勝手に直列化すると、
# 「壊れないこと」ではなく「競合しなかったこと」を測ってしまう)。
while time.time() < start_at:
    time.sleep(0.001)
paths = " ".join(files)
# 事故の窓そのもの: add してから commit までに間が空く。
line = f'git add {paths} && sleep {gap} && git commit -q -m "{subject}"'
if mode == "unsafe":
    cmd = ["sh", "-c", line]
else:
    cmd = [sys.executable, tool, "-C", repo, "--wait", "30"]
    if mode == "nowait":
        cmd.append("--nowait")
    cmd += ["--sh", line]
t0 = time.time()
r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
print(json.dumps({"role": role, "rc": r.returncode, "sec": round(time.time() - t0, 2),
                  "err": r.stderr.strip()[-300:]}, ensure_ascii=False))
"""


def _mk_repo(td: Path) -> Path:
    repo = td / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    run = lambda *c: subprocess.run(c, cwd=repo, check=True,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "test@example.invalid")
    run("git", "config", "user.name", "selftest")
    run("git", "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("検査用\n", encoding="utf-8")
    run("git", "add", "README.md")
    run("git", "commit", "-q", "-m", "init")
    return repo


def _commits(repo: Path) -> list:
    """init 以降のコミットを (題, 含むファイル) で並べる。"""
    out = subprocess.run(["git", "log", "--format=%H%x00%s", "--reverse"],
                         cwd=repo, capture_output=True, text=True, check=True)
    rows = []
    for ln in out.stdout.splitlines():
        sha, subj = ln.split("\0", 1)
        if subj == "init":
            continue
        names = subprocess.run(["git", "show", "--name-only", "--format=", sha],
                               cwd=repo, capture_output=True, text=True, check=True)
        rows.append({"subject": subj,
                     "files": sorted(x for x in names.stdout.split() if x)})
    return rows


def _mismatch(rows: list) -> list:
    """題と中身が食い違ったコミットを数える。"""
    bad = []
    for r in rows:
        want = "assets/" if r["subject"] == SUBJ_A else "notes/"
        foreign = [f for f in r["files"] if not f.startswith(want)]
        if foreign or not r["files"]:
            bad.append({"subject": r["subject"], "foreign": foreign,
                        "files": r["files"]})
    return bad


def _reinject(td: Path, mode: str, *, gap: float = 1.5) -> dict:
    """事故と同じ手順を2プロセスで同時に起こす。

    A: 9ファイルを add → gap 秒あく → 自分の題で commit
    B: A の窓のまん中で自分の1ファイルを add → 自分の題で commit
    """
    repo = _mk_repo(td)
    tool = str(Path(__file__).resolve())
    start = time.time() + 1.0
    procs = []
    for role, files, subj, offset in (("a", NINE, SUBJ_A, 0.0),
                                      ("b", [OTHER], SUBJ_B, gap / 2)):
        procs.append(subprocess.Popen(
            [sys.executable, "-c", _CHILD, str(repo), role, f"{start + offset:.3f}",
             "0.05" if role == "b" else str(gap), mode, tool,
             json.dumps(files), subj],
            stdout=subprocess.PIPE, text=True))
    res = {}
    for pr in procs:
        out, _ = pr.communicate()
        d = json.loads(out.strip().splitlines()[-1])
        res[d["role"]] = d
    rows = _commits(repo)
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                            cwd=repo, capture_output=True, text=True, check=True)
    return {"mode": mode, "commits": rows, "mismatch": _mismatch(rows),
            "a": res["a"], "b": res["b"],
            "left_staged": sorted(x for x in staged.stdout.split() if x)}


def _show(r: dict) -> None:
    for c in r["commits"]:
        print(f"     - 題「{c['subject']}」 中身 {len(c['files'])}件: "
              f"{', '.join(c['files'][:3])}{' …' if len(c['files']) > 3 else ''}")
    print(f"     A: rc={r['a']['rc']} {r['a']['sec']}s / "
          f"B: rc={r['b']['rc']} {r['b']['sec']}s / 食い違い {len(r['mismatch'])}本")


def selftest() -> int:
    ng = []

    def case(name, fn):
        try:
            fn()
        except AssertionError as e:
            ng.append(f"{name}: {e}")
            print(f"NG {name}: {e}")
        else:
            print(f"ok {name}")

    print("== 事故の再注入(9ファイルを add した直後に別プロセスが commit)==")

    with tempfile.TemporaryDirectory() as t:
        print("\n[1] ロック無し —— 事故がいま再現するか(再現しないなら検査が測れていない)")
        bad = _reinject(Path(t), "unsafe")
        _show(bad)

        def reproduces():
            assert bad["mismatch"], ("ロック無しで事故が再現しない。"
                                     "窓が狭すぎて競合していない疑い")
            assert bad["a"]["rc"] != 0, ("A が空振りしていない。事故では A の commit が "
                                         "「no changes added」で落ちる")
        case("ロック無しで題と中身が食い違う(事故の再現)", reproduces)
        base_b = bad["b"]["sec"]   # 待たなかったときの B の所要(比較の物差し)

    with tempfile.TemporaryDirectory() as t:
        print("\n[2] ロック有り(既定=待つ)—— 後から来た方が待つ")
        ok = _reinject(Path(t), "lock")
        _show(ok)

        def clean():
            assert not ok["mismatch"], f"食い違いが残った: {ok['mismatch']}"
            assert len(ok["commits"]) == 2, f"コミットが2本でない: {len(ok['commits'])}"
            assert ok["a"]["rc"] == 0 and ok["b"]["rc"] == 0, "どちらかが失敗した"
            assert not ok["left_staged"], f"staged が残った: {ok['left_staged']}"
        case("食い違い0本・2本とも自分の中身だけ", clean)

        def waited():
            # B が待てるのは「窓に入った時刻から A が抜けるまで」で、gap の半分が上限。
            assert ok["b"]["sec"] >= 0.5, (
                f"B が待っていない({ok['b']['sec']}s / 待たないと {base_b}s)。"
                "錠が効いていない疑い")
        case(f"後から来た B が先客を待った(待たないと {base_b}s)", waited)

    with tempfile.TemporaryDirectory() as t:
        print("\n[3] --nowait —— 後から来た方が明示的に断られる")
        no = _reinject(Path(t), "nowait")
        _show(no)

        def refused():
            assert no["b"]["rc"] == BUSY_EXIT, (
                f"B が {BUSY_EXIT} で断られていない: rc={no['b']['rc']}")
            assert not no["mismatch"], f"食い違いが残った: {no['mismatch']}"
            assert len(no["commits"]) == 1, f"コミットが1本でない: {len(no['commits'])}"
        case("B は 75 で断られ、A の9ファイルだけが1本になる", refused)

    print("\n== 結果 ==")
    if ng:
        print(f"NG {len(ng)}件")
        return 1
    print("すべて ok —— 待つ側は待ち、断る側は 75 で断り、題と中身の食い違いは0本")
    return 0


if __name__ == "__main__":
    sys.exit(main())
