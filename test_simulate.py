# -*- coding: utf-8 -*-
"""foldersync 双机模拟测试：用两个本地目录模拟两台电脑，共享目录模拟远端。"""
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from foldersync import Engine

PASSPHRASE = "test-口令-123"


def make_cfg(folder, remote, state, device, passphrase=PASSPHRASE):
    return {
        "local_folder": str(folder),
        "device": device,
        "profile": "test",
        "passphrase": passphrase,
        "state_dir": str(state),
        "transport": {"type": "localdir", "remote_dir": str(remote)},
    }


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def tree(folder):
    folder = Path(folder)
    out = {}
    for f in sorted(folder.rglob("*")):
        if f.is_file():
            out[f.relative_to(folder).as_posix()] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def expect(cond, msg):
    if not cond:
        raise AssertionError("FAIL: " + msg)
    print("  ok -", msg)


def main():
    root = Path(tempfile.mkdtemp(prefix="foldersync-test-"))
    remote = root / "remote"
    A, B = root / "machineA", root / "machineB"
    stA, stB = root / "stateA", root / "stateB"
    ca = make_cfg(A, remote, stA, "A-pc")
    cb = make_cfg(B, remote, stB, "B-pc")
    ea, eb = Engine(ca), Engine(cb)

    print("场景 1：A 新建文件，首次上传")
    write(A / "offer letter.docx", "offer 正文 v1，含中文")
    write(A / "子目录/notes.txt", "笔记内容")
    ea.run_once()
    expect(len(ea.fetch_metas()) == 2, "远端出现 2 条文件记录")

    print("场景 2：B 首次同步，拉到全部文件")
    eb.run_once()
    expect(tree(A) == tree(B), "两台机器内容完全一致")
    expect((B / "子目录/notes.txt").read_text(encoding="utf-8") == "笔记内容", "子目录文件内容正确")

    print("场景 3：A 修改，B 更新")
    write(A / "offer letter.docx", "offer 正文 v2（A 修改）")
    ea.run_once(); eb.run_once()
    expect((B / "offer letter.docx").read_text(encoding="utf-8").startswith("offer 正文 v2"), "B 拿到新版本")
    expect(tree(A) == tree(B), "内容仍一致")

    print("场景 4：A 删除文件，B 跟随删除")
    (A / "子目录/notes.txt").unlink()
    ea.run_once(); eb.run_once()
    expect(not (B / "子目录/notes.txt").exists(), "B 上文件已删除")
    expect(tree(A) == tree(B), "内容一致")

    print("场景 5：两边同时改同一文件 → 冲突保留两个版本")
    write(A / "offer letter.docx", "A 的独立修改")
    write(B / "offer letter.docx", "B 的独立修改")
    ea.run_once()  # A 先推自己的版本
    eb.run_once()  # B 发现冲突：存下 A 的版本，推自己的版本
    ea.run_once()  # A 拉到 B 的版本和冲突副本
    ta, tb = tree(A), tree(B)
    expect(ta == tb, "冲突后两台机器内容仍一致")
    contents = {p: (A / p).read_text(encoding="utf-8") for p in ta}
    expect("A 的独立修改" in contents.values(), "A 的修改被保留（冲突副本）")
    expect("B 的独立修改" in contents.values(), "B 的修改被保留（正式版本）")

    print("场景 6：两边各建不同新文件，互通")
    write(A / "来自A.txt", "aaa")
    write(B / "来自B.pdf", b"\%PDF-1.4 binary \x00\x01\x02")
    ea.run_once(); eb.run_once(); ea.run_once()
    expect((B / "来自A.txt").exists() and (A / "来自B.pdf").exists(), "新文件互相到达")
    expect(tree(A) == tree(B), "内容一致")

    print("场景 7：两边创建同名同内容文件 → 不产生冲突副本")
    write(A / "same.txt", "完全一样")
    write(B / "same.txt", "完全一样")
    ea.run_once(); eb.run_once(); ea.run_once()
    conflicts = [p for p in tree(A) if "冲突" in p and p.startswith("same")]
    expect(not conflicts and tree(A) == tree(B), "无冲突副本且一致")

    print("场景 8：错误口令被拒绝")
    try:
        Engine(make_cfg(B, remote, root / "stateX", "X-pc", passphrase="错的"))
        expect(False, "应当抛出口令错误")
    except SystemExit as e:
        expect("口令" in str(e), "报错信息正确: %s" % e)

    print("场景 9：本地删除 + 远端修改 → 远端内容恢复")
    (A / "来自A.txt").unlink()          # A 删除
    ea.run_once()
    write(B / "来自A.txt", "远端改过的新内容")  # B 随后修改
    eb.run_once()
    ea.run_once()                        # A 发现"我删了但远端改过" → 恢复
    expect((A / "来自A.txt").exists() and
           (A / "来自A.txt").read_text(encoding="utf-8") == "远端改过的新内容",
           "修改过的文件被恢复（数据不丢）")

    print("\n全部 9 个场景通过 ✔")
    shutil.rmtree(str(root), ignore_errors=True)


if __name__ == "__main__":
    main()
