# git-index-lock

[日本語](README.md) | [English](README.en.md)

When several AI agents (Claude Code, Codex, and so on) or scripts work in the same Git worktree at the same time, another process can slip in between your `git add` and your `git commit`. The files someone else staged then end up in your commit, or yours end up in theirs. git-index-lock is a small wrapper that runs add-through-commit under one lock so that cannot happen.

## What goes wrong

The Git index is a single shared resource per worktree.

1. Process A runs `git add` on nine files.
2. Before A commits, process B adds its own file and commits.
3. B's commit now contains A's nine files, and A's commit fails with "no changes added".

You are left with a commit whose message does not match its contents. If the messages happen to be similar, nobody notices.

## Requirements

- Linux or macOS (uses `fcntl.flock`; Windows is not supported)
- Python 3.8+ (standard library only)
- Git

## Usage

Put `git_lock.py` anywhere and run it. Always pass add and commit in **one call**; splitting them into two calls reopens the window.

```sh
# Run add and commit as one critical section
python3 git_lock.py --sh 'git add -A src && git commit -m "Update src"'

# Pass a git command directly (the leading "git" may be omitted for add/commit/diff/log/show/status)
python3 git_lock.py -- git commit --amend -m "..."
python3 git_lock.py -- status --short

# Refuse immediately with exit code 75 if someone else holds the lock
python3 git_lock.py --nowait --sh 'git add -A && git commit -m "..."'

# Target a repository in another directory
python3 git_lock.py -C path/to/repo -- status
```

- By default it waits for the current holder (up to 600 seconds, configurable with `--wait`) and exits with 75 on timeout.
- While waiting or refusing, it prints the holder's pid and command to stderr.
- The lock is an `flock` on `<git-dir>/index-guard.lock`. The OS releases it when the process dies, so stale locks never remain.
- Nested calls that try to take the same lock while it is held are detected through the `GIT_INDEX_LOCK_HELD` environment variable and pass straight through instead of deadlocking.
- The wrapped command's exit code is returned as is. A missing executable returns 127 and a non-executable file returns 126.

To make agents use it, add a line such as "run git add/commit only through `git_lock.py --sh '...'`, in a single call" to your `AGENTS.md` or `CLAUDE.md`.

## Limitations

- It only protects git operations that go through this wrapper. Git commands run without it are not protected.
- Separate worktrees created with `git worktree` have separate indexes and do not have this problem in the first place.

## Tests

```sh
python3 git_lock.py --self-test          # reproduce the accident in a throwaway repo, then show the lock prevents it
python3 -m unittest discover -s tests    # CLI regression tests
```

`--self-test` first confirms that, without the lock, a commit with mismatched message and contents really is produced. It then checks that with the lock there are zero mismatched commits, and that with `--nowait` the later process is refused with 75.

Messages printed by the tool are in Japanese.

## License

MIT
