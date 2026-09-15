# -*- coding: utf-8 -*-
"""
foldersync —— 两台电脑之间的文件夹同步工具（v1）

设计要点：
- 通道可插拔：webdav（坚果云等）/ localdir（任意本地或共享目录，可用于
  SMB 共享、Tailscale 虚拟局域网内的另一台电脑、移动硬盘等）
- 端到端加密：口令经 scrypt 派生密钥，文件内容与元数据均用 AES-256-GCM
  加密后才离开本机，中转方看不到文件名与内容
- 删除用"墓碑"记录，能区分"对方删了文件"和"对方还没见过这个新文件"
- 冲突不猜测：两边都改过同一文件时，保留两个版本，其中一份以
  "xxx (冲突来自 设备名 时间).ext" 副本形式存在
- 所有写入先落临时文件再原子替换，中断不会留下半个文件

限制（v1 已知）：
- 文件改名按"删除 + 新建"处理，另一端会重新下载
- 不同步空目录；大文件不做增量传输
- 配置文件里存有加密口令，它保护的是"云端的存储"，不保护能直接
  读你硬盘的人
"""

import argparse
import base64
import hashlib
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

# 本工具只用 AES-GCM/scrypt（都在 OpenSSL 默认 provider 里），
# PyInstaller 打包环境下 legacy provider 可能缺失，直接禁用以免报错
os.environ.setdefault("CRYPTOGRAPHY_OPENSSL_NO_LEGACY", "1")

# ---------------------------------------------------------------- 基础工具

def log(msg):
    line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg
    print(line, flush=True)
    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "foldersync.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def state_dir(profile="default"):
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "foldersync" / profile


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 512), b""):
            h.update(chunk)
    return h.hexdigest()


def sanitize_relpath(p):
    p = p.replace("\\", "/")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts or any(x == ".." or ":" in x or "\x00" in x for x in parts):
        raise ValueError("非法路径: %r" % p)
    return "/".join(parts)


def conflict_name(path, device):
    d, base = path.rsplit("/", 1) if "/" in path else ("", path)
    stem, ext = os.path.splitext(base)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = "%s (冲突来自 %s %s)%s" % (stem, device, stamp, ext)
    return d + "/" + name if d else name


# ---------------------------------------------------------------- 加密

MAGIC = b"FS1"
KDF_N, KDF_R, KDF_P = 2 ** 14, 8, 1


def derive_key(passphrase, salt):
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    kdf = Scrypt(salt=salt, length=32, n=KDF_N, r=KDF_R, p=KDF_P)
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt(key, fileid, plaintext):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, plaintext, fileid.encode("utf-8"))


def decrypt(key, fileid, blob):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if blob[:3] != MAGIC:
        raise ValueError("数据格式不正确（可能不是 foldersync 的数据）")
    return AESGCM(key).decrypt(blob[3:15], blob[15:], fileid.encode("utf-8"))


def load_or_create_kdf(tr, passphrase):
    if tr.get("kdf.json") is None:
        tr.ensure_dirs()
    raw = tr.get("kdf.json")
    if raw is None:
        salt = os.urandom(16)
        key = derive_key(passphrase, salt)
        kdf = {
            "salt": base64.b64encode(salt).decode(),
            "check": base64.b64encode(encrypt(key, "kdf-check", b"foldersync-ok")).decode(),
            "kdf": {"n": KDF_N, "r": KDF_R, "p": KDF_P},
        }
        tr.put("kdf.json", json.dumps(kdf).encode("utf-8"))
        return key
    kdf = json.loads(raw.decode("utf-8"))
    key = derive_key(passphrase, base64.b64decode(kdf["salt"]))
    try:
        decrypt(key, "kdf-check", base64.b64decode(kdf["check"]))
    except Exception:
        raise SystemExit("加密口令错误：与远端已有数据不匹配，请检查 config 里的 passphrase")
    return key


# ---------------------------------------------------------------- 传输通道

class LocalTransport:
    """把任意本地/共享目录当作远端（SMB 共享、Tailscale 网内目录、移动硬盘等）"""

    def __init__(self, cfg):
        self.root = Path(cfg["remote_dir"]).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, path):
        return self.root.joinpath(*path.split("/"))

    def ensure_dirs(self):
        for d in ("meta", "data"):
            self._p(d).mkdir(parents=True, exist_ok=True)

    def list_etags(self, subpath):
        p = self._p(subpath)
        if not p.is_dir():
            return {}
        out = {}
        for x in p.iterdir():
            if x.is_file():
                out[x.name] = str(x.stat().st_mtime)
        return out

    def get(self, path):
        p = self._p(path)
        if not p.is_file():
            return None
        with open(p, "rb") as f:
            return f.read()

    def put(self, path, data):
        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".__fs_tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, str(p))
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def delete(self, path):
        try:
            self._p(path).unlink()
        except FileNotFoundError:
            pass


class WebDAVTransport:
    """标准 WebDAV 通道（坚果云：https://dav.jianguoyun.com/dav/）"""

    def __init__(self, cfg):
        import requests
        from urllib.parse import urlparse
        self.requests = requests
        self.base = cfg["url"].rstrip("/") + "/"
        u = urlparse(self.base)
        self.origin = "%s://%s" % (u.scheme, u.netloc)
        self.s = requests.Session()
        self.s.auth = (cfg["username"], cfg["password"])
        self.s.headers["User-Agent"] = "foldersync/1.0"

    def _url(self, path):
        from urllib.parse import quote
        return self.base + quote(path)

    def _exists(self, abs_path):
        r = self.s.request("PROPFIND", self.origin + abs_path,
                           headers={"Depth": "0"}, timeout=90)
        return r.status_code in (200, 207)

    def _request(self, method, path, **kw):
        r = self.s.request(method, self._url(path), timeout=90, **kw)
        if r.status_code == 401:
            raise SystemExit("坚果云拒绝了登录（HTTP 401）：username 应为登录邮箱，"
                             "password 应为「应用密码」而不是登录密码。"
                             "获取方法：网页版 → 账户信息 → 安全选项 → 添加应用密码")
        return r

    def ensure_dirs(self):
        from urllib.parse import urlparse
        segs = [x for x in urlparse(self.base).path.split("/") if x]
        for i in range(1, len(segs) + 1):
            abs_path = "/" + "/".join(segs[:i]) + "/"
            r = self.s.request("MKCOL", self.origin + abs_path, timeout=90)
            if r.status_code not in (200, 201, 301, 405):
                if r.status_code == 403 and self._exists(abs_path.rstrip("/")):
                    continue  # 坚果云对已有目录/根目录返回 403 而非 405
                raise RuntimeError("创建远端目录 %s 失败: HTTP %d %s"
                                   % ("/".join(segs[:i]), r.status_code, r.text[:200]))
        for d in ("meta", "data"):
            r = self._request("MKCOL", d + "/")
            if r.status_code not in (200, 201, 405):
                if r.status_code == 403 and self._exists(
                        urlparse(self._url(d + "/")).path.rstrip("/")):
                    continue
                raise RuntimeError("创建远端目录 %s 失败: HTTP %d %s"
                                   % (d, r.status_code, r.text[:200]))

    def list_etags(self, subpath):
        """返回 {文件名: etag}，供元数据缓存判断哪些文件变了。"""
        body = ("<?xml version=\"1.0\"?><D:propfind xmlns:D=\"DAV:\">"
                "<D:prop><D:resourcetype/><D:getetag/></D:prop></D:propfind>")
        r = self._request("PROPFIND", subpath + "/", headers={"Depth": "1"}, data=body)
        if r.status_code in (404, 409):
            return {}
        if r.status_code not in (200, 207):
            raise RuntimeError("列出远端目录 %s 失败: HTTP %d" % (subpath, r.status_code))
        import xml.etree.ElementTree as ET
        from urllib.parse import unquote
        root = ET.fromstring(r.content)
        out, me = {}, unquote(subpath).rstrip("/").split("/")[-1]
        for resp in root.iter("{DAV:}response"):
            href = unquote(resp.findtext("{DAV:}href") or "")
            name = href.rstrip("/").split("/")[-1]
            if not name or name == me:
                continue
            if resp.find("{DAV:}propstat/{DAV:}prop/{DAV:}resourcetype/{DAV:}collection") is None:
                out[name] = resp.findtext("{DAV:}propstat/{DAV:}prop/{DAV:}getetag")
        return out

    def get(self, path):
        r = self._request("GET", path)
        if r.status_code in (404, 409):  # 409 = 坚果云在上级目录不存在时返回
            return None
        if r.status_code != 200:
            raise RuntimeError("下载 %s 失败: HTTP %d" % (path, r.status_code))
        return r.content

    def put(self, path, data):
        r = self._request("PUT", path, data=data)
        if r.status_code not in (200, 201, 204):
            raise RuntimeError("上传 %s 失败: HTTP %d %s" % (path, r.status_code, r.text[:200]))

    def delete(self, path):
        r = self._request("DELETE", path)
        if r.status_code not in (200, 204, 404, 409):
            raise RuntimeError("删除 %s 失败: HTTP %d" % (path, r.status_code))


def accumulate_segments(path):
    parts = [x for x in path.split("/") if x]
    return ["/".join(parts[:i + 1]) for i in range(len(parts))]


def make_transport(cfg):
    t = cfg["transport"]["type"]
    if t == "localdir":
        return LocalTransport(cfg["transport"])
    if t == "webdav":
        return WebDAVTransport(cfg["transport"])
    raise SystemExit("未知通道类型: %s（支持 webdav / localdir）" % t)


# ---------------------------------------------------------------- 同步引擎

SKIP_NAMES = ("desktop.ini", "thumbs.db", ".ds_store")


def should_skip(name):
    return (name.startswith("~$") or name.endswith(".tmp")
            or name.startswith(".__fs_tmp") or name.lower() in SKIP_NAMES)


class Engine:
    def __init__(self, cfg, verbose=True):
        self.cfg = cfg
        self.folder = Path(os.path.expandvars(
            os.path.expanduser(cfg["local_folder"]))).resolve()
        if not self.folder.is_dir():
            self.folder.mkdir(parents=True, exist_ok=True)
            log("本地文件夹不存在，已自动创建：%s" % self.folder)
        self.device = cfg.get("device") or socket.gethostname()
        profile = cfg.get("profile", "default")
        self.state = Path(cfg["state_dir"]) if cfg.get("state_dir") else state_dir(profile)
        self.state.mkdir(parents=True, exist_ok=True)
        self.index_path = self.state / "index.json"
        self.verbose = verbose
        self.tr = make_transport(cfg)
        self.key = load_or_create_kdf(self.tr, cfg["passphrase"])

    # ---- 本地扫描

    def scan(self):
        out = {}
        for dirpath, _dirnames, filenames in os.walk(self.folder):
            for fn in filenames:
                if should_skip(fn):
                    continue
                full = os.path.join(dirpath, fn)
                rel = sanitize_relpath(os.path.relpath(full, self.folder))
                try:
                    st = os.stat(full)
                    out[rel] = {"size": st.st_size, "mtime": st.st_mtime,
                                "hash": sha256_file(full)}
                except OSError:
                    pass  # 正被占用或已消失，下一轮再说
        return out

    # ---- 索引

    def load_index(self):
        if self.index_path.exists():
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        return {"files": {}}

    def save_index(self, index):
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(str(tmp), str(self.index_path))

    # ---- 远端元数据

    def fileid(self, relpath):
        return hashlib.sha256(relpath.encode("utf-8")).hexdigest()[:32]

    def fetch_metas(self):
        # ETag 缓存：元数据没变的不再重复下载（省流量，也省坚果云的请求次数配额）
        cache_path = self.state / "metas_cache.json"
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
        entries = self.tr.list_etags("meta")
        metas = {}
        for name, etag in entries.items():
            m = re.fullmatch(r"([0-9a-f]{32})\.json", name)
            if not m:
                continue
            hit = cache.get(name)
            if hit is not None and hit.get("etag") == etag:
                metas[hit["meta"]["path"]] = hit["meta"]
                continue
            blob = self.tr.get("meta/" + name)
            if blob is None:
                continue
            try:
                meta = json.loads(decrypt(self.key, m.group(1), blob).decode("utf-8"))
                meta["path"] = sanitize_relpath(meta["path"])
            except Exception as e:
                log("警告：无法解析远端元数据 %s（%s）" % (name, e))
                continue
            cache[name] = {"etag": etag, "meta": meta}
        live = set(entries)
        for name in list(cache):
            if name not in live:
                del cache[name]
        try:
            cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        result = {}
        for hit in cache.values():
            meta = hit["meta"] if isinstance(hit, dict) and "meta" in hit else hit
            old = result.get(meta["path"])
            if old is None or (meta.get("updated", 0), meta["version"]) > \
                    (old.get("updated", 0), old["version"]):
                result[meta["path"]] = meta
        return result

    # ---- 远端操作

    def push(self, path, info, version):
        fid = self.fileid(path)
        with open(self.folder.joinpath(*path.split("/")), "rb") as f:
            data = f.read()
        self.tr.put("data/" + fid, encrypt(self.key, fid, data))
        meta = {"path": path, "hash": info["hash"], "size": info["size"],
                "mtime": info["mtime"], "version": version, "deleted": False,
                "device": self.device, "updated": time.time()}
        self.tr.put("meta/%s.json" % fid,
                    encrypt(self.key, fid, json.dumps(meta, ensure_ascii=False).encode("utf-8")))
        return meta

    def push_tombstone(self, path, version):
        fid = self.fileid(path)
        self.tr.delete("data/" + fid)
        meta = {"path": path, "hash": None, "size": 0, "mtime": 0,
                "version": version, "deleted": True,
                "device": self.device, "updated": time.time()}
        self.tr.put("meta/%s.json" % fid,
                    encrypt(self.key, fid, json.dumps(meta, ensure_ascii=False).encode("utf-8")))
        return meta

    def pull(self, meta, fid=None):
        fid = fid or self.fileid(meta["path"])
        blob = self.tr.get("data/" + fid)
        if blob is None:
            raise RuntimeError("远端数据缺失: %s" % meta["path"])
        data = decrypt(self.key, fid, blob)
        if hashlib.sha256(data).hexdigest() != meta["hash"]:
            raise RuntimeError("远端数据校验失败: %s" % meta["path"])
        target = self.folder.joinpath(*meta["path"].split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".__fs_tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, str(target))
        os.utime(str(target), (meta["mtime"], meta["mtime"]))

    def delete_local(self, path):
        try:
            (self.folder.joinpath(*path.split("/"))).unlink()
        except FileNotFoundError:
            pass

    def has_local_changes(self):
        """快速判断有没有"改了但还没上传"的内容（纯本地比对，不联网）。"""
        try:
            scan = self.scan()
            files = self.load_index()["files"]
        except Exception:
            return True  # 拿不准就当作有变化，宁可多同步一次
        for path, info in scan.items():
            idx = files.get(path)
            if idx is None or idx.get("deleted") or idx.get("hash") != info["hash"]:
                return True
        for path, idx in files.items():
            if not idx.get("deleted") and path not in scan:
                return True  # 本地删除还没上传
        return False

    # ---- 锁：防止两台机器同时跑同步

    def acquire_lock(self):
        token = json.dumps({"device": self.device, "ts": time.time()})
        deadline = time.time() + 90
        while time.time() < deadline:
            raw = self.tr.get("lock.json")
            if raw is None:
                self.tr.put("lock.json", token.encode("utf-8"))
                again = self.tr.get("lock.json")
                if again is not None and again.decode("utf-8") == token:
                    self._locked = True
                    return True
            else:
                try:
                    info = json.loads(raw.decode("utf-8"))
                except Exception:
                    info = {}
                if info.get("device") == self.device or time.time() - info.get("ts", 0) > 120:
                    self.tr.delete("lock.json")
                    continue
            time.sleep(3)
        raise RuntimeError("获取同步锁超时（另一台电脑可能正在同步）")

    def release_lock(self):
        self.tr.delete("lock.json")
        self._locked = False

    # ---- 主流程

    def run_once(self):
        t0 = time.time()
        before = self.load_index()
        index = {"files": {k: dict(v) for k, v in before["files"].items()}}
        scan = self.scan()
        stats = {"push": 0, "pull": 0, "delete": 0, "conflict": 0, "noop": 0}

        self.acquire_lock()
        acquired = getattr(self, "_locked", False)
        try:
            metas = self.fetch_metas()
            conflict_pushes = []  # 冲突副本（本轮新建的本地文件）

            for path in sorted(set(scan) | set(index["files"]) | set(metas)):
                l = scan.get(path)
                idx = index["files"].get(path)
                r = metas.get(path)
                iv = idx["version"] if idx else 0
                rv = r["version"] if r else 0
                l_hash = l["hash"] if l else None
                r_hash = r["hash"] if (r and not r.get("deleted")) else None
                l_changed = l is not None and (idx is None or idx.get("deleted")
                                               or idx["hash"] != l_hash)
                l_gone = l is None and idx is not None and not idx.get("deleted")
                r_changed = (r is not None and rv > iv and r_hash is not None
                             and (idx is None or idx.get("deleted")
                                  or r_hash != idx.get("hash")))
                r_gone = r is not None and rv > iv and r.get("deleted")

                if l_hash is not None and l_hash == r_hash:
                    # 内容一致：仅对齐版本号
                    index["files"][path] = {"hash": l_hash,
                                            "size": l["size"] if l else (r.get("size", 0)),
                                            "mtime": l["mtime"] if l else (r.get("mtime", 0)),
                                            "version": max(iv, rv)}
                    stats["noop"] += 1

                elif l_changed and (r_changed or r_gone) and r_hash is not None:
                    # 两边都改（或本地改了、远端被删但远端曾有内容）：保留两个版本
                    cf = conflict_name(path, r.get("device", "远端"))
                    cf = sanitize_relpath(cf)
                    self.pull(dict(r, path=cf), fid=self.fileid(path))
                    fid_cf = self.fileid(cf)
                    conflict_pushes.append((cf, scan_entry_of(self.folder, cf), 1))
                    self.push(path, l, max(iv, rv) + 1)
                    index["files"][path] = dict(l, version=max(iv, rv) + 1)
                    log("冲突：两边都改了 %s，远端版本已存为 %s" % (path, cf.rsplit("/", 1)[-1]))
                    stats["conflict"] += 1

                elif l_changed:
                    self.push(path, l, max(iv, rv) + 1)
                    index["files"][path] = dict(l, version=max(iv, rv) + 1)
                    stats["push"] += 1

                elif r_changed:
                    if l_gone:
                        # 本地删了、远端却被改过：以远端内容恢复（数据安全优先）
                        log("恢复：本地已删除但远端有修改 %s，已还原" % path)
                    self.pull(r)
                    index["files"][path] = {"hash": r["hash"], "size": r.get("size", 0),
                                            "mtime": r.get("mtime", 0), "version": rv}
                    stats["pull"] += 1

                elif l_gone and not r_gone:
                    self.push_tombstone(path, max(iv, rv) + 1)
                    index["files"][path] = {"deleted": True, "version": max(iv, rv) + 1}
                    stats["delete"] += 1

                elif r_gone and not l_gone and not l_changed:
                    self.delete_local(path)
                    index["files"][path] = {"deleted": True, "version": rv}
                    stats["delete"] += 1

                elif l_gone and r_gone:
                    stats["noop"] += 1  # 两边都删了，墓碑保留

                else:
                    stats["noop"] += 1

            for cf, info, ver in conflict_pushes:
                if info is None:
                    continue
                self.push(cf, info, ver)
                index["files"][cf] = dict(info, version=ver)
        finally:
            if acquired:
                self.release_lock()

        self.save_index(index)
        if self.verbose:
            log("同步完成：推送 %d、拉取 %d、删除 %d、冲突 %d（%.1f 秒）"
                % (stats["push"], stats["pull"], stats["delete"],
                   stats["conflict"], time.time() - t0))
        return stats


def scan_entry_of(folder, relpath):
    full = folder.joinpath(*relpath.split("/"))
    try:
        st = os.stat(str(full))
        return {"size": st.st_size, "mtime": st.st_mtime, "hash": sha256_file(str(full))}
    except OSError:
        return None


# ---------------------------------------------------------------- 命令行

def load_config(path):
    if not os.path.exists(path):
        raise SystemExit("未找到配置文件 %s，请把 config.example.toml 复制为 "
                         "config.toml 并按注释填写" % path)
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        import tomllib  # Python 3.11+
        return tomllib.loads(text)
    except ImportError:
        return parse_toml_lite(text)


def parse_toml_lite(text):
    """只支持本工具需要的 TOML 子集：[节] + key = '字面量字符串' / "基本字符串" / 数字"""
    cfg, section = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            cfg[section] = cfg.get(section, {})
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if v[:1] in ("'", '"'):
            q = v[0]
            end = v.find(q, 1)
            if q == "'" and v[end:end + 2] == "''":
                end = v.find("'", end + 2)  # literal 字符串的 '' 转义
            while q == '"' and v[end - 1] == "\\" and v[end - 2] != "\\":
                end = v.find(q, end + 1)    # 基本字符串的 \" 转义
            if end == -1:
                raise ValueError("配置文件引号不配对：%s" % line)
            raw_val = v[1:end]
            val = (raw_val.replace("''", "'") if q == "'"
                   else raw_val.replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n"))
        else:
            cut = v.find("#")
            v = v[:cut] if cut != -1 else v
            v = v.strip()
            try:
                val = int(v)
            except ValueError:
                val = v
        target = cfg if section is None else cfg[section]
        target[k] = val
    return cfg


def cmd_init(cfg):
    eng = Engine(cfg)
    eng.run_once()
    log("初始化完成。在另一台电脑上做同样配置后运行 init，即可开始同步。")


def cmd_once(cfg):
    Engine(cfg).run_once()


def cmd_check(cfg):
    eng = Engine(cfg)
    metas = eng.fetch_metas()
    log("连接正常。远端共 %d 个文件记录。" % len(metas))


def cmd_run(cfg, interval):
    import threading as th
    eng = Engine(cfg)
    busy = th.Lock()

    def cycle():
        if not busy.acquire(blocking=False):
            return
        try:
            eng.run_once()
        except Exception as e:
            log("同步出错：%r（将继续重试）" % e)
        finally:
            busy.release()

    cycle()  # 启动先同步一次
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        class H(FileSystemEventHandler):
            def __init__(self):
                self.timer = None

            def on_any_event(self, event):
                if event.is_directory:
                    return
                name = os.path.basename(event.src_path)
                if should_skip(name):
                    return
                if self.timer:
                    self.timer.cancel()
                self.timer = th.Timer(2.0, cycle)
                self.timer.daemon = True
                self.timer.start()

        obs = Observer()
        obs.schedule(H(), str(eng.folder), recursive=True)
        obs.daemon = True
        obs.start()
        log("实时监视已启动：%s（另有每 %d 秒的全量扫描兜底）"
            % (eng.folder, interval))
    except ImportError:
        log("未安装 watchdog，退化为定时扫描模式（pip install watchdog 可开启实时同步）")

    while True:
        time.sleep(interval)
        cycle()


def main():
    ap = argparse.ArgumentParser(description="foldersync —— 双机文件夹同步")
    ap.add_argument("command", choices=["init", "once", "run", "check"],
                    help="init=首次初始化 once=同步一次 run=常驻实时同步 check=测试连接")
    ap.add_argument("--config", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.toml"))
    ap.add_argument("--interval", type=int, default=600,
                    help="run 模式的全量扫描间隔秒数，默认 600")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.command == "init":
        cmd_init(cfg)
    elif args.command == "once":
        cmd_once(cfg)
    elif args.command == "check":
        cmd_check(cfg)
    else:
        cmd_run(cfg, args.interval)


if __name__ == "__main__":
    main()
