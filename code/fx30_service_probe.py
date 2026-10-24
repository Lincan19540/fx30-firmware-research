#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FX30 服务模式只读探测（公开版 v1）
=================================
将 Sony FX30 从标准 MSC 存储设备身份（054C:0E0E）切换为服务模式（054C:0336），
完成 Senser 认证并读取 HASP 服务令牌（88 字节）。

核心修正：MSC 信息查询与 Senser 认证必须使用不同 USB 句柄。

依赖：
  - pyusb / libusb_package
  - Sony 相机 USB 协议封装库（pmca / transport，第三方开源库，请按上游安装）
  通过 FX30_DEPS 环境变量指定协议库路径，例如：
    sudo PYTHONPATH="$(FX30_DEPS):$(FX30_DEPS)/vendor" python3 fx30_service_probe.py

仅用于学术研究，不用于商业行为；操作风险自负。
"""
import os, sys, time, subprocess, plistlib
from pathlib import Path

# 协议库路径：默认取环境变量 FX30_DEPS；未设置时尝试从脚本同级目录查找
_DEPS = os.environ.get("FX30_DEPS", "")
if _DEPS:
    sys.path.insert(0, _DEPS)
    sys.path.insert(0, str(Path(_DEPS) / "vendor"))
else:
    _cand = Path(__file__).resolve().parent.parent / "deps"
    if _cand.exists():
        sys.path.insert(0, str(_cand))
        sys.path.insert(0, str(_cand / "vendor"))

import usb.core, usb.util, usb.backend.libusb1
import libusb_package
backend = usb.backend.libusb1.get_backend(find_library=libusb_package.find_library)

import transport
from transport import Bounded, get_device
from pmca.usb.driver.generic import MscBbbDriver, GenericUsbDriver
from pmca.usb import MscDevice
from pmca.usb.sony import (SonyExtCmdCamera, SonyMscExtCmdDevice,
                           SonySenserAuthDevice, SonySenserDevice,
                           SonySenserCamera)

VID = 0x054C
PID_MSC = 0x0E0E


def step(m):
    print(f"\n=== {m} ===", flush=True)


def list_sony():
    return {d.idProduct: d for d in usb.core.find(find_all=True, idVendor=VID,
                                                  backend=backend)}


def mac_unmount(pid):
    """macOS：卸载相机存储卷（整个磁盘，非单个卷）"""
    print(f"  卸载 054C:{pid:04X} 挂载卷...", flush=True)
    try:
        tree = plistlib.loads(subprocess.run(
            ["/usr/sbin/ioreg", "-p", "IOService", "-r", "-c",
             "IOUSBHostDevice", "-l", "-a"],
            capture_output=True, timeout=30).stdout)
    except Exception as e:
        print(f"  [warn] ioreg: {e}")
        return

    def walk(n):
        if isinstance(n, list):
            for x in n:
                yield from walk(x)
        elif isinstance(n, dict):
            yield n
            yield from walk(n.get("IORegistryEntryChildren", []))

    seen = set()
    for node in walk(tree):
        if node.get("idVendor") == VID and node.get("idProduct") == pid:
            for med in walk(node):
                if med.get("IOObjectClass") == "IOMedia" and med.get("BSD Name"):
                    d = med["BSD Name"]
                    if "s" in d[4:] or d in seen:
                        continue
                    seen.add(d)
                    r = subprocess.run(["/usr/sbin/diskutil", "unmountDisk",
                                        f"/dev/{d}"],
                                       capture_output=True, text=True,
                                       timeout=15)
                    print(f"    {d}: {'OK' if r.returncode == 0 else r.stderr.strip()}",
                          flush=True)


def claim_fresh(pid):
    """重新 find + detach + claim，返回干净的 device"""
    dev = get_device(pid)
    if dev.is_kernel_driver_active(0):
        dev.detach_kernel_driver(0)
    usb.util.claim_interface(dev, 0)
    return dev


def main():
    step("0. 前置检查")
    if sys.platform == "darwin" and os.geteuid() != 0:
        print("  需要 sudo")
        sys.exit(2)

    step("1. 扫描 Sony USB")
    before = list_sony()
    for pid, d in before.items():
        try:
            sn = usb.util.get_string(d, d.iSerialNumber)
            prod = usb.util.get_string(d, d.iProduct)
        except Exception:
            sn = prod = "?"
        print(f"  054C:{pid:04X}  {prod}  S/N={sn}", flush=True)
        usb.util.dispose_resources(d)
    if PID_MSC not in before:
        print(f"  [FAIL] 没看到 054C:{PID_MSC:04X}")
        sys.exit(1)

    if sys.platform == "darwin":
        step("2. Mac 卸载 MSC")
        mac_unmount(PID_MSC)
        time.sleep(1)

    # ---- 阶段 A：MSC 信息查询（独立句柄，读完即 dispose）----
    step("3. [句柄A] MSC 读相机型号")
    try:
        dev_a = claim_fresh(PID_MSC)
        msc = MscBbbDriver(Bounded(dev_a, 15))
        info = MscDevice(msc).getDeviceInfo()
        print(f"  manufacturer: {info.manufacturer.strip()!r}", flush=True)
        ext = SonyExtCmdCamera(SonyMscExtCmdDevice(msc))
        ci = ext.getCameraInfo()
        print(f"  modelName: {ci.modelName!r}", flush=True)
        print(f"  serial:    {ci.serial!r}", flush=True)
    except Exception as e:
        print(f"  [warn] MSC 查询: {e}", flush=True)
    finally:
        usb.util.dispose_resources(dev_a)
        time.sleep(0.5)

    # ---- 阶段 B：Senser 认证（全新句柄，不做任何 SCSI）----
    step("4. [句柄B] 发 Senser 认证（干净句柄，无 SCSI 命令）")
    try:
        dev_b = claim_fresh(PID_MSC)
        print(f"  bus={dev_b.bus} addr={dev_b.address}", flush=True)
        auth = SonySenserAuthDevice(GenericUsbDriver(Bounded(dev_b, 20)))
        auth.start()
        auth.authenticate()
        print("  *** 认证握手完成！***", flush=True)
        auth.stop()
    except Exception as e:
        print(f"  [FAIL] 认证: {e}", flush=True)
        try:
            auth.stop()
        except Exception:
            pass
        usb.util.dispose_resources(dev_b)
        sys.exit(1)
    finally:
        usb.util.dispose_resources(dev_b)

    # ---- 阶段 C：等新 PID ----
    step("5. 等新 Sony PID 出现（25 秒）")
    deadline = time.monotonic() + 25
    new_pid = None
    new_dev = None
    while time.monotonic() < deadline:
        time.sleep(0.5)
        cur = list_sony()
        for pid, d in cur.items():
            if pid != PID_MSC:
                new_pid = pid
                new_dev = d
                break
        if new_dev:
            break
    if new_dev is None:
        print("  [FAIL] 相机没切身份", flush=True)
        sys.exit(1)
    print(f"  [OK] 新身份 054C:{new_pid:04X}", flush=True)

    # ---- 阶段 D：新 PID 上认证 + 读 HASP ----
    step(f"6. 在 054C:{new_pid:04X} 上认证+读 HASP")
    time.sleep(2)
    try:
        new_dev.set_configuration()
    except Exception:
        pass
    try:
        sa = SonySenserAuthDevice(GenericUsbDriver(Bounded(new_dev, 60)))
        sa.start()
        sa.authenticate()
        ch = SonySenserDevice(sa.driver)
        cam = SonySenserCamera(ch)
        hasp = cam.readHasp()
        print(f"  HASP 长度: {len(hasp)}", flush=True)
        print(f"  HASP hex:  {hasp[:40].hex()}", flush=True)
        if len(hasp) == 88:
            print("  *** FX30 服务通道可用！***", flush=True)
        sa.stop()
    except Exception as e:
        print(f"  [FAIL] 二次认证: {e}", flush=True)
    finally:
        usb.util.dispose_resources(new_dev)
    step("完成")


if __name__ == "__main__":
    main()
