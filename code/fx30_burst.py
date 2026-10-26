#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fx30_burst.py —— FX30 隐藏连拍开启 / 回滚工具（公开版）

在 BK4 设置容器里成对写入两个字节，重算 bank 校验和后写回：

    0x8EF80 = 0x01   EXTERN_VAL_DRIVE_MODE_*（属性 0x02cf013b）→ BURST
    0x91690 = 0x01   DRIVE_MODE_*          （属性 0x02cf02ae）→ CONT_SHOOTING

两个字节必须**成对写**；写后必须**拔 USB → 取电池 ≥30 秒 → 开机**才生效
（设置区是 warm-boot 镜像，热重启会还原）。

用法：
  sudo python3 fx30_burst.py --probe            # 只读：快照 + 校验和 + 当前档位
  sudo python3 fx30_burst.py --burst --dry-run  # 演练：只算不写
  sudo python3 fx30_burst.py --burst --yes      # 实写 → 断电重启
  sudo python3 fx30_burst.py --single --yes     # 回滚到单张
  sudo python3 fx30_burst.py --restore fx30_dump/bk4_snapshot_xxx.bin

依赖（两条）：
  1. pyusb / libusb_package
  2. Sony 相机 USB 协议封装库（pmca / transport，第三方开源，不在本仓库分发）
     用 FX30_DEPS 指定其路径：
       export FX30_DEPS=/path/to/a7c2-fl23-research/src/camera_runtime
       sudo -E PYTHONPATH="$FX30_DEPS:$FX30_DEPS/vendor" python3 fx30_burst.py --probe

安全：只写设置容器（2.25MB，bank 校验和自洽才写），不碰固件分区；
每次写入前自动落快照，读回不符合预期自动回滚。风险与免责见 README。
"""
import os
import sys
import time
import struct
import hashlib
import argparse
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEPS = Path(os.environ.get("FX30_DEPS") or (HERE.parent / "deps"))
for p in (DEPS / "vendor", DEPS):
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

DUMP = Path.cwd() / "fx30_dump"
try:
    DUMP.mkdir(parents=True, exist_ok=True)
except Exception:
    DUMP = Path("/tmp")   # 离线自检时无写权限，退到临时目录

PFUNC_ADJ = 0x40
EXT = ['SINGLE', 'BURST', 'BRACKET', 'REMOTE', 'SPEED_PRIOR', 'RAW_BURST',
       'SINGLE_BRACKET', 'SELF10S', 'SELF2S', 'BURST_SELF10S', 'INVALID',
       'SELF3S', 'SELF5S', 'BURST_SELF2S', 'BURST_SELF5S', 'BOL_BURST', 'SPOT_BURST']
INT = ['1_IMAGE', 'CONT', 'CONT_LIMITED', 'SPD_PRIOR_CONT', 'SINGLE_BURST',
       'SELF_TIMER', 'SELF_PORTRAIT', 'CONT_SELF_TIMER', 'CONT_BRACKET',
       'BRACKET_SINGLE', 'FOCUS_BRACKET', 'WB_BRACKET', 'DRO_BRACKET',
       'LPF_BRACKET', 'REMOTE_CDR', 'BURST_SHOOTING', 'MOTION_SHOT']

MAGIC = b'BK4\0'

# ---------------- 写入门禁（改这里必须是有意识的决定）----------------
# 只允许改「驱动模式」这两个枚举位置字节。每一项都必须有书面理由。
#
# 0x8EF80 / 0x91690 —— 依据 `fx30-senser-map/FX30-连拍复现实操手册.md` §12：
#   这两个字节**不是属性 id**，而是两张枚举表的 0-based 位置：
#     0x8EF80 -> EXTERN_VAL_DRIVE_MODE_*（属性 0x02cf013b），0x01 = BURST
#     0x91690 -> DRIVE_MODE_*           （属性 0x02cf02ae），0x01 = CONT_SHOOTING
#   必须**成对写**（同一设置的两层表示，缺一会被固件回退）。
#   两者都在 bank0（< 0xd2560），共改 2 字节，校验和随 bank0 重算。
#   已于 2026-10-26 07:35 实机验证：写入 → 断电重启 → 菜单显示「连拍」→ 可连续拍摄。
#
# 已移除（错误方向，2026-10-26 清理）：
#   0x915D2..0x915D7 —— 曾用于属性 0x02cf0273 的实验，实测证明该属性与连拍无关。
# 已移除（2026-10-26 实测证伪，勿再启用）：
#   · 「7 字节等值组」28 字节 —— 演练反查证明是 PDT 打包对齐的巧合（1B属性+填充+4B属性+填充），
#     不是语义数组；三个组结构完全相同。
#   · 6 条"静态能力标志"（0x02cf02ce/cf、0x02cf0350/51、0x02cf04b7/b8）—— 实机成对写入后
#     菜单无任何变化，已逐一排除。
# 结论：菜单可见性不在 BK4 属性层。

ALLOWED_OFFSETS = {0x8EF80, 0x91690}
# 属性级放行：默认空（只按偏移放行）；如需按属性 id 放行，在这里显式加
ALLOWED_PROP_IDS = set()
# 单次会话允许改动的字节上限（超过即拒绝，防止手滑/脚本错）
MAX_CHANGED_BYTES = 8
LOG_PATH = None
LOG = []


# ---------------------------------------------------------------- 日志
def p(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    LOG.append(line)


def dump_log():
    if LOG_PATH:
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(LOG) + "\n")
        print("日志 -> %s" % LOG_PATH, flush=True)


# ---------------------------------------------------------------- BK4
def bank_checksum(bank: bytes) -> int:
    return (sum(bank[:0x20]) + sum(bank[0x24:])) & 0xFFFFFFFF


def parse_banks(data: bytes):
    out, pos = [], 0
    while True:
        i = data.find(MAGIC, pos)
        if i < 0:
            break
        off = i - 0x0C
        if off < 0:
            break
        ns, npr, start, size = struct.unpack_from("<4I", data, off + 0x10)
        ck = struct.unpack_from("<I", data, off + 0x20)[0]
        end = off + start + size
        if end > len(data):
            break
        out.append({"off": off, "end": end, "ns": ns, "np": npr,
                    "ck": ck, "calc": bank_checksum(data[off:end])})
        pos = off + start + size
    if not out:
        raise RuntimeError("找不到 BK4 bank —— 读回内容不是 BK4 容器")
    return out


def check(data: bytes, tag=""):
    banks = parse_banks(data)
    bad = [b for b in banks if b["ck"] != b["calc"]]
    for n, b in enumerate(banks):
        p("    bank%d off=%#08x end=%#08x ns=%-4d np=%-5d ck=%#010x calc=%#010x %s"
          % (n, b["off"], b["end"], b["ns"], b["np"], b["ck"], b["calc"],
             "OK" if b["ck"] == b["calc"] else "MISMATCH"))
    if bad:
        p("  [!] %s 校验和不自洽：%d 个 bank" % (tag, len(bad)))
    return not bad, banks


def apply_edits(data: bytearray, edits):
    banks = parse_banks(bytes(data))
    changed, touched = [], set()
    for off, val in edits:
        hit = None
        for idx, b in enumerate(banks):
            if b["off"] <= off < b["end"]:
                hit = idx
                break
        if hit is None:
            raise RuntimeError("偏移 %#x 不在任何 bank 内" % off)
        old = data[off]
        if old != val:
            data[off] = val
            changed.append((off, old, val))
            touched.add(hit)
    for idx in sorted(touched):
        b = parse_banks(bytes(data))[idx]
        ck = bank_checksum(bytes(data[b["off"]:b["end"]]))
        struct.pack_into("<I", data, b["off"] + 0x20, ck)
        p("  [ck] bank%d checksum -> %#010x" % (idx, ck))
    return changed, sorted(touched)


def parse_props(blob):
    """解析 bank 内的属性表：pid(子系统<<16|局部序号) -> (attr, capacity, 绝对偏移)"""
    props = {}
    for b in parse_banks(bytes(blob)):
        base = b["off"]
        d = bytes(blob[base:b["end"]])
        ns = b["ns"]
        table = 0x100 + ns * 6
        for s in range(ns):
            count, first = struct.unpack_from("<HI", d, 0x100 + s * 6)
            for k in range(count):
                pos = table + (first + k) * 6
                if pos + 6 > len(d):
                    continue
                attr, packed = struct.unpack_from("<HI", d, pos)
                if packed == 0xFFFFFFFF:
                    continue
                size, off = packed >> 24, packed & 0xFFFFFF
                cap = size
                if size == 0xFF:
                    size = cap = struct.unpack_from("<H", d, off)[0]
                    off += 2
                elif size == 0:
                    size, cap = struct.unpack_from("<HH", d, off)
                    off += 4
                if off + cap > len(d):
                    continue
                props[(s << 16) | k] = (attr, cap, base + off)
    return props


def props_to_edits(blob, pairs):
    """--prop 0x02cf0273=0x01 -> 按属性容量展开成字节编辑（容量 4 则 4 字节同值）"""
    props = parse_props(blob)
    edits = []
    for pid, val in pairs:
        if pid not in props:
            raise RuntimeError("属性 %#010x 不在容器内" % pid)
        attr, cap, off = props[pid]
        for k in range(cap):
            edits.append((off + k, val))
        p("  [prop] %#010x attr=%#06x cap=%d off=%#08x <- %#04x ×%d"
          % (pid, attr, cap, off, val, cap))
        # 同步 attr 0xC0 的「四副本孪生项」（若紧邻且同值）
        twin = pid + 1
        if cap == 1 and twin in props and props[twin][0] == 0xC0:
            a2, c2, o2 = props[twin]
            for k in range(c2):
                edits.append((o2 + k, val))
            p("  [twin] %#010x attr=%#06x cap=%d off=%#08x <- %#04x ×%d（孪生项同步）"
              % (twin, a2, c2, o2, val, c2))
    return edits


def prop_of_offset(blob, off):
    """反查某个字节偏移属于哪个 BK4 属性，用于写前影响预览"""
    try:
        for pid, (attr, cap, o) in parse_props(blob).items():
            if o <= off < o + cap:
                return pid, attr, cap, o
    except Exception:
        pass
    return None


def state(blob, tag):
    e, i = blob[0x8EF80], blob[0x91690]
    p("  [%s] 0x8EF80=%#04x %-12s | 0x91690=%#04x %s"
      % (tag, e, EXT[e] if e < len(EXT) else "?",
         i, INT[i] if i < len(INT) else "?"))


# ---------------------------------------------------------------- 相机会话
def _claim(dev):
    """接管相机 USB 接口：强 set_configuration → 打印接口表 → 多次 detach/claim 重试"""
    import usb.util
    cfg = dev.get_active_configuration()          # macOS 上等价于强制 set_configuration
    for itf in cfg:
        p("    intf#%d alt=%d class=%02x/%02x/%02x nEP=%d"
          % (itf.bInterfaceNumber, itf.bAlternateSetting, itf.bInterfaceClass,
             itf.bInterfaceSubClass, itf.bInterfaceProtocol, itf.bNumEndpoints))
    last = None
    for attempt in (1, 2, 3):
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
                p("    detach_kernel_driver(0) OK")
            usb.util.claim_interface(dev, 0)
            p("  [+] 已接管接口 0（第 %d 次尝试）" % attempt)
            return
        except Exception as e:
            last = e
            p("  [!] 接管第 %d 次失败: %r" % (attempt, e))
            try:
                usb.util.dispose_resources(dev)
            except Exception:
                pass
            time.sleep(1.5)
    raise RuntimeError("无法接管相机 USB 接口（3 次）：%r\n"
                       "  提示：拔掉 USB 重插，相机屏幕选「影像传输(MSC)」，"
                       "并先关掉「照片」「图像捕捉」等占用程序。" % (last,))


class Session:
    def __init__(self):
        self.dev = None
        self.ch = None

    def __enter__(self):
        import usb.core
        import usb.util
        import usb.backend.libusb1
        import libusb_package
        import transport
        from pmca.usb.driver.generic import GenericUsbDriver
        from pmca.usb.sony import SonySenserAuthDevice, SonySenserDevice

        backend = usb.backend.libusb1.get_backend(find_library=libusb_package.find_library)
        try:
            import macos_host
            macos_host.unmount_camera()
        except Exception:
            pass
        time.sleep(1)

        dev = None
        for k in range(120):
            if k % 10 == 0:
                p("    等 MSC 0e0e … %ds" % (k // 2))
            try:
                dev = usb.core.find(idVendor=0x054C, idProduct=0x0E0E, backend=backend)
            except Exception:
                dev = None
            if dev is not None:
                break
            time.sleep(0.5)
        if dev is None:
            raise RuntimeError("没看到 054C:0E0E —— 相机 USB 设为「影像传输(MSC)」并插线")
        p("  [+] MSC 已就绪，第 1 阶段认证 …")
        _claim(dev)
        a = SonySenserAuthDevice(GenericUsbDriver(transport.Bounded(dev, 20)))
        a.start()
        a.authenticate()          # 不 stop：stop 会破坏后续 0x603 通道
        usb.util.dispose_resources(dev)

        svc = None
        for k in range(50):
            if k % 10 == 0:
                p("    等服务身份 0336 … %ds" % (k // 2))
            for d in usb.core.find(find_all=True, idVendor=0x054C,
                                   idProduct=0x0336, backend=backend):
                svc = d
                break
            if svc:
                break
            time.sleep(0.5)
        if svc is None:
            raise RuntimeError("没出现服务身份 054C:0336")
        time.sleep(2)
        self.dev = svc
        self.sa = SonySenserAuthDevice(GenericUsbDriver(transport.Bounded(svc, 300)))
        self.sa.start()
        self.sa.authenticate()
        self.ch = SonySenserDevice(self.sa.driver)
        p("  [+] 服务通道建立")

        from pmca.usb.sony import SonySenserCamera
        try:
            hasp = SonySenserCamera(self.ch).readHasp()
            n = len(hasp) if hasp else 0
            p("  [HASP] %d B %s" % (n, "[OK]" if n == 88 else "[!] 会话可能不完整"))
        except Exception as e:
            p("  [HASP] 读取异常 %r" % (e,))
        return self

    def __exit__(self, *exc):
        try:
            if self.dev:
                import usb.util
                usb.util.dispose_resources(self.dev)
        except Exception:
            pass

    # -- 0x603 原语 --------------------------------------------------
    def read_pdt(self):
        h = struct.pack("<HH", 0x603, 5)
        res, data = self.ch.sendSenserPacket(PFUNC_ADJ, h + b"\x01")
        p("  [read] 0x603/5 res=%s len=%d" % (res, len(data) if data else 0))
        if res != 0 or not data:
            raise RuntimeError("读取 PDT 失败 res=%s" % res)
        return bytearray(data)

    def write_pdt(self, blob):
        h = struct.pack("<HH", 0x603, 4)
        t0 = time.time()
        res, _ = self.ch.sendSenserPacket(PFUNC_ADJ, h + struct.pack("<I", 0) + bytes(blob))
        p("  [write] 0x603/4 res=%s %.1fs" % (res, time.time() - t0))
        return res

    def save(self):
        h = struct.pack("<HH", 0x603, 3)
        res, _ = self.ch.sendSenserPacket(PFUNC_ADJ, h + struct.pack("<H", 0))
        p("  [save ] 0x603/3 res=%s" % res)
        return res


# ---------------------------------------------------------------- 主流程
def do_probe(s):
    p("\n--- 1. 读回 BK4 容器 ---")
    blob = s.read_pdt()
    stamp = time.strftime("%m%d_%H%M%S")
    snap = DUMP / ("bk4_snapshot_%s.bin" % stamp)
    snap.write_bytes(blob)
    p("  [+] 快照 %s (%d B, sha256=%s)"
      % (snap.name, len(blob), hashlib.sha256(blob).hexdigest()[:16]))
    p("\n--- 2. 本地校验和校验 ---")
    ok, _ = check(blob, "读回")
    state(blob, "当前")
    return blob, snap, ok


def do_write(s, blob, edits, dry_run, yes=False, snapshot=None):
    before = bytearray(blob)
    p("\n--- 3. 打补丁 + 重算校验和 ---")
    changed, touched = apply_edits(blob, edits)
    if not changed:
        p("  目标字节已是目标值，无需写入")
        return 0
    for off, old, val in changed:
        p("  %#08x: %#04x -> %#04x" % (off, old, val))

    p("\n  [影响预览] 本次改动落在这些 BK4 属性上：")
    seen = set()
    for off, old, val in changed:
        info = prop_of_offset(before, off)
        if info:
            pid, attr, cap, base = info
            if pid not in seen:
                seen.add(pid)
                cur = bytes(before[base:base + cap]).hex()
                p("    属性 %#010x attr=%#06x cap=%d 当前值=%s" % (pid, attr, cap, cur))
        else:
            p("    %#08x 不属于任何已知属性（危险，需人工确认）" % off)

    # 白名单：允许两种来源 —— 显式偏移，或属于放行属性 ID 的字节
    allowed_prop_offsets = set()
    for _pid, (_attr, _cap, _base) in parse_props(bytes(before)).items():
        if _pid in ALLOWED_PROP_IDS:
            allowed_prop_offsets.update(range(_base, _base + _cap))
    bad = [off for off, _, _ in changed
           if off not in ALLOWED_OFFSETS and off not in allowed_prop_offsets]
    if bad:
        p("  [门禁拒绝] 以下偏移不在白名单内：%s" % ", ".join("%#x" % b for b in bad))
        p("             如需放开，必须先改 ALLOWED_OFFSETS 并说明理由。")
        return 5
    if len(changed) > MAX_CHANGED_BYTES:
        p("  [门禁拒绝] 单次改动 %d 字节，超过上限 %d" % (len(changed), MAX_CHANGED_BYTES))
        return 5

    ok, _ = check(bytes(blob), "补丁后")
    if not ok:
        p("  [ABORT] 补丁后校验和仍不自洽 —— 拒绝写入")
        return 2
    state(blob, "补丁后")

    if dry_run:
        out = DUMP / ("bk4_dryrun_%s.bin" % time.strftime("%m%d_%H%M%S"))
        out.write_bytes(blob)
        p("  [DRY-RUN] 未写入；候选镜像 -> %s" % out.name)
        return 0

    p("")
    p("  ┌─ 写入前风险声明 ─────────────────────────────────────────")
    p("  │ 写入对象：设置容器（BK4，2.25MB），**不碰固件分区**")
    p("  │ 本次改动：%d 字节（上面列出的偏移）" % len(changed))
    p("  │ 回滚材料：%s" % (snapshot.name if snapshot else "无（危险）"))
    p("  │ 断电风险：写入/保存期间断电可能造成设置区半写")
    p("  │           → 写后会自动读回校验，异常立即自动回滚")
    p("  │ 最坏情况：设置区损坏 → 开机异常或回出厂（固件完好）")
    p("  └────────────────────────────────────────────────────")
    if not yes:
        p("  [安全闸] 未加 --yes，本轮不写入。确认无误后加 --yes 重跑。")
        return 0

    p("\n--- 4. 写入 + save ---")
    res = s.write_pdt(blob)
    if res != 0:
        p("  [FAIL] 写被拒 res=%s" % res)
        p("  对照：res=129 = 固件校验拒绝（校验和/内容）；res=0 = 接受")
        return 3
    s.save()
    time.sleep(1.0)

    p("\n--- 5. 读回验证 ---")
    after = s.read_pdt()
    snap = DUMP / ("bk4_after_%s.bin" % time.strftime("%m%d_%H%M%S"))
    snap.write_bytes(after)
    state(after, "写后")
    check(bytes(after), "写后读回")
    diffs = [i for i in range(min(len(before), len(after))) if before[i] != after[i]]
    p("  与写前差异 %d 字节: %s" % (len(diffs), ", ".join("%#x" % i for i in diffs[:20])))

    hit = all(after[o] == v for o, _, v in changed)
    p("  目标字节%s" % ("★ 已生效" if hit else "!! 未生效（被固件归一/回写）"))

    if (not hit or not check(bytes(after), "写后读回")[0]) and snapshot:
        p("  [自动回滚] 读回不符合预期，正在写回写前快照 …")
        try:
            rb = bytearray(snapshot.read_bytes())
            if check(bytes(rb), "回滚镜像")[0] and s.write_pdt(rb) == 0:
                s.save()
                time.sleep(1.0)
                back = s.read_pdt()
                p("  [自动回滚] %s" % ("已恢复（读回一致）" if bytes(back) == bytes(rb)
                                        else "恢复后读回不一致，请立刻停止并告知我"))
            else:
                p("  [自动回滚] 快照校验不过或写入被拒，未回滚")
        except Exception as e:
            p("  [自动回滚] 失败：%r" % (e,))
    p("\n--- 6. 下一步（人工）---")
    p("  1) 拔 USB → 取电池 ≥30 秒 → 装回开机（nflasha5 是 warm-boot 镜像，热重启会还原）")
    p("  2) 看 MENU → 拍摄 → 驱动模式：是否出现「连拍」，且「单张」仍在")
    p("  3) 回滚：--restore %s" % snap.name)
    return 0 if hit else 4


def do_restore(s, path):
    blob = bytearray(Path(path).read_bytes())
    p("\n--- 恢复镜像 %s ---" % path)
    ok, _ = check(bytes(blob), "恢复镜像")
    if not ok:
        p("  [ABORT] 镜像校验和不自洽，拒绝写入")
        return 2
    res = s.write_pdt(blob)
    if res != 0:
        p("  [FAIL] res=%s" % res)
        return 3
    s.save()
    time.sleep(1.0)
    after = s.read_pdt()
    diffs = [i for i in range(min(len(after), len(blob))) if after[i] != blob[i]]
    same = not diffs
    if not same:
        # 容器是"活"的：机身会周期回写计数器区，逐字节完全相等几乎永远不会成立。
        # 因此不再把"不等"一律当失败，而是把差异偏移打出来，由人判断落在哪一区。
        snap = DUMP / ("bk4_restore_after_%s.bin" % time.strftime("%m%d_%H%M%S"))
        snap.write_bytes(after)
        p("  读回差异 %d 字节: %s%s"
          % (len(diffs), ", ".join("%#x" % i for i in diffs[:24]),
             " …" if len(diffs) > 24 else ""))
        p("  读回快照 -> %s（供离线比对）" % snap.name)
        # 关键：恢复镜像里的目标字节是否已回到镜像值
        p("  提示：落在计数器区（如 0x2c / 0x6e9dc / 0xd3009）的差异属机身自写，"
          "不代表恢复失败；请用上方偏移判断。")
    p("  读回%s一致" % ("完全" if same else "不完全"))
    return 0 if same else 4


def main():
    global LOG_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--apply", nargs="*", metavar="OFF=VAL")
    ap.add_argument("--prop", nargs="*", metavar="ID=VAL")
    ap.add_argument("--list", nargs="?", const="0x02cf", metavar="SUBSYS")
    ap.add_argument("--restore", metavar="FILE")
    ap.add_argument("--burst", action="store_true", help="开启连拍（= --apply 0x8EF80=0x01 0x91690=0x01）")
    ap.add_argument("--single", action="store_true", help="回滚单张（= --apply 0x8EF80=0x00 0x91690=0x00）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true", help="确认实际写入（否则只演练）")
    args = ap.parse_args()

    if not (args.probe or args.apply is not None or args.prop is not None
            or args.list or args.restore or args.burst or args.single):
        print(__doc__)
        return 2

    LOG_PATH = DUMP / ("session_%s.log" % time.strftime("%Y%m%d_%H%M%S"))
    p("=" * 78)
    p("FX30 BK4 写入工具 rev6（安全闸+自动回滚） —— %s" % " ".join(sys.argv[1:]))
    p("时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    p("=" * 78)

    try:
        with Session() as s:
            if args.restore:
                return do_restore(s, args.restore)
            blob, snap, ok = do_probe(s)
            if args.probe:
                p("\n--- 只读结束，未写入任何数据 ---")
                return 0 if ok else 1
            edits = []
            if args.burst:
                edits += [(0x8EF80, 0x01), (0x91690, 0x01)]
            if args.single:
                edits += [(0x8EF80, 0x00), (0x91690, 0x00)]
            for a in (args.apply or []):
                k, v = a.split("=", 1)
                edits.append((int(k, 0), int(v, 0) & 0xFF))
            if args.prop:
                pairs = []
                for a in args.prop:
                    k, v = a.split("=", 1)
                    pairs.append((int(k, 0), int(v, 0) & 0xFF))
                edits.extend(props_to_edits(blob, pairs))
            if args.list:
                want = int(args.list, 0)
                p("\n--- 属性列表 supervisor %#06x ---" % want)
                for pid, (attr, cap, off) in sorted(parse_props(blob).items()):
                    if (pid >> 16) != want:
                        continue
                    p("  %#010x attr=%#06x cap=%-3d off=%#08x %s"
                      % (pid, attr, cap, off, bytes(blob[off:off + cap]).hex()))
                return 0
            return do_write(s, blob, edits, args.dry_run, args.yes, snap)
    except Exception as e:
        import traceback
        p("[!] %r" % (e,))
        p(traceback.format_exc())
        return 1
    finally:
        dump_log()


if __name__ == "__main__":
    sys.exit(main() or 0)
