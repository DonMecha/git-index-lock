# git-index-lock

[日本語](README.md) | [English](README.en.md)

同じワークツリーで複数のAIエージェント（Claude Code、Codexなど）やスクリプトを同時に動かすと、`git add` から `git commit` までの間に別のプロセスが割り込むことがあります。その結果、他のプロセスがstageしたファイルが自分のコミットに混ざります。git-index-lockは、addからcommitまでを1つのロックの下で実行し、この割り込みを防ぐ小さなラッパーです。

## 何が起きるのか

Gitのインデックスはワークツリーに1つしかない共有資源です。

1. Aが9ファイルを `git add` する
2. Aが `git commit` する前に、Bが自分のファイルを add して commit する
3. BのコミットにAの9ファイルが含まれ、Aの commit は「no changes added」で空振りする

こうして、題と中身が食い違ったコミットが残ります。題が似ていれば、混ざったことに誰も気づきません。

## 動作要件

- Linux または macOS（`fcntl.flock` を使います。Windowsは非対応）
- Python 3.8以降（標準ライブラリだけで動きます）
- Git

## 使い方

`git_lock.py` を好きな場所に置いて実行します。addとcommitは必ず**1回の呼び出し**にまとめます。2回に分けると、その間にまた割り込まれます。

```sh
# addとcommitを1区間で実行する
python3 git_lock.py --sh 'git add -A src && git commit -m "Update src"'

# gitコマンドをそのまま渡す（add/commit/diff/log/show/statusは先頭のgitを省略可）
python3 git_lock.py -- git commit --amend -m "..."
python3 git_lock.py -- status --short

# 先客がいたら待たずに終了コード75で断る
python3 git_lock.py --nowait --sh 'git add -A && git commit -m "..."'

# 別のディレクトリのリポジトリを対象にする
python3 git_lock.py -C path/to/repo -- status
```

- 既定では先客が終わるまで待ちます（上限600秒、`--wait` で変更可）。上限を超えたら終了コード75で終わります。
- 待つときと断るときは、先客のpidとコマンドを標準エラーに出します。
- ロックは `<git-dir>/index-guard.lock` への `flock` です。プロセスが落ちればOSが解放するので、古いロックは残りません。
- ロック中に同じロックを取ろうとする入れ子の呼び出しは、環境変数 `GIT_INDEX_LOCK_HELD` で判別して素通しします。自分自身を待ち続ける状態にはなりません。
- 実行したコマンドの終了コードはそのまま返します。実行ファイルが見つからない場合は127、実行権限がない場合は126です。

エージェントに使わせる場合は、`AGENTS.md` や `CLAUDE.md` に「gitのadd・commitは `git_lock.py --sh '...'` 経由で1回にまとめる」と書いておきます。

## 制約

- 防げるのは、このラッパー経由のgit操作どうしの割り込みだけです。ラッパーを通さないgit操作は防げません。
- `git worktree` で分けた別ワークツリーはインデックスも別なので、そもそもこの問題は起きません。

## 検査

```sh
python3 git_lock.py --self-test          # 事故を使い捨てリポジトリで再現し、ロック有りで起きないことを確認
python3 -m unittest discover -s tests    # CLIの回帰検査
```

`--self-test` は、ロック無しでは題と中身が食い違うコミットが実際にできることを先に確かめます。そのうえで、ロック有りでは食い違いが0本になることと、`--nowait` で後から来た側が75で断られることを確認します。

## ライセンス

MIT
