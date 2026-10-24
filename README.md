# FX30 固件研究项目（FX30 Firmware Research Project）

> Sony FX30（ILCE-FX30）固件纯学术研究记录。当前公开：**服务模式连接全链路**（含可运行代码）。其余研究阶段暂不公开。

## 支持机型

> **本项目仅针对 Sony FX30（ILCE-FX30）。** 固件逆向与设备修改存在**变砖风险**，可能使设备失去保修。所有操作**风险自负**，请仅在自有设备上、在理解全部风险的前提下进行。出错时请先停止，不要盲目重试写入。

## Contents

- [重要声明](#重要声明)
- [已公开内容](#已公开内容)
- [环境与依赖](#环境与依赖)
- [开始前注意](#开始前注意)
- [服务模式连接：分步指南](#服务模式连接分步指南)
- [故障排查](#故障排查)
- [验证状态](#验证状态)
- [命令行与开发](#命令行与开发)
- [许可与独立研究声明](#许可与独立研究声明)
- [支持本项目](#支持本项目)

---

## 重要声明

- 本项目**仅供学术研究与技术学习**，**不用于任何商业行为**，禁止用于盈利、侵权或非法用途。
- 本仓库**不包含任何 Sony 固件二进制、加密密钥或受版权保护内容**，仅包含研究方法、流程记录与主机侧代码。
- 请尊重 Sony 知识产权与用户协议；如 Sony 或其授权方要求，本项目相关内容将立即下架。

---

## 已公开内容

| 功能 | 说明 |
| --- | --- |
| **服务模式连接** | 通过 USB 将 FX30 从标准 MSC 存储设备身份（`054C:0E0E`）切换为 Sony 服务模式（`054C:0336`），完成 Senser 认证并读取 HASP 服务令牌（88 字节）——后续一切只读取证与固件研究的地基 |

- 💻 代码：[`code/fx30_service_probe.py`](code/fx30_service_probe.py)

## 环境与依赖

| 项 | 说明 |
| --- | --- |
| 主机 | macOS（脚本含 `ioreg` / `diskutil` 卸载逻辑，可适配其他平台） |
| 运行权限 | Python 3，需 `sudo` 运行以访问 USB 层 |
| 依赖 | `pyusb` + `libusb_package`，以及 Sony 相机 USB 协议封装库（`pmca` / `transport`，第三方开源库，不在本仓库内分发，请按上游说明安装） |

```bash
sudo PYTHONPATH="<协议库路径>:<依赖路径>" python3 code/fx30_service_probe.py
```

## 开始前注意

- 相机电量不低于 50%。
- 连接过程相机可能显示「请检查连接的设备」、电脑可能不出现盘符——均为正常现象，以脚本日志为准。
- 仅当相机以 **Mass Storage（存储）** 模式接入时执行。

## 服务模式连接：分步指南

```
Step 1 枚举 MSC 054C:0E0E（相机以存储设备身份接入）
Step 2 卸载存储卷（macOS：diskutil unmountDisk）
Step 3 detach_kernel_driver(0) + claim_interface(0)
Step 4 独立句柄读相机型号（MSC SCSI：getDeviceInfo / getCameraInfo）
Step 5 全新句柄发 Senser 认证（auth.start / authenticate）
        ↓ 相机自动断 USB 重枚举
Step 6 等待约 25 秒，新身份 054C:0336 出现
Step 7 重新认证 + 读取 HASP 88 字节 → 服务通道可用 ✓
```

### Step 1–3：枚举与准备

1. 枚举：`usb.core.find(find_all=True, idVendor=0x054C)`，确认 `0x0E0E` 出现。
2. 卸载（macOS）：通过 `ioreg` 遍历 IOUSBHostDevice 树，找到相机的 IOMedia 节点，对其 BSD Name 执行 `diskutil unmountDisk`（卸载整个磁盘，而非单个卷）。
3. 释放内核驱动：`dev.is_kernel_driver_active(0)` → `dev.detach_kernel_driver(0)` → `usb.util.claim_interface(dev, 0)`，得到一个干净句柄。

### Step 4：读取相机型号（句柄 A）

用 `MscBbbDriver` + `MscDevice` 读设备信息，再用 Sony 扩展命令读 `modelName` / `serial`。读完立即 `dispose_resources`。

### Step 5：Senser 认证（句柄 B，全新句柄）

`SonySenserAuthDevice` → `auth.start()` → `auth.authenticate()`。认证成功时相机自动断开并重枚举成新 PID。

### Step 6–7：等新 PID，二次认证读 HASP

轮询 25 秒，等待非 `0x0E0E` 的 Sony PID 出现（即 `054C:0336`）；在新身份上重新 `auth.start()` / `authenticate()`，经 `SonySenserCamera.readHasp()` 读取 88 字节 HASP 令牌。长度等于 88 即服务通道可用。

## 故障排查

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| MSC 查询后认证失败 | 同一 USB 句柄先做 SCSI 再发认证，bulk 端点状态被污染 | 查询与认证**必须用不同句柄**；查询句柄用完即 dispose |
| macOS 上认不出设备 | 内核驱动占用接口 | 必须先 `detach_kernel_driver(0)` |
| 卸载报错 | 卸载了单个卷而非整个磁盘 | 用 `diskutil unmountDisk` |
| 认证后等不到 0336 | 重枚举需要时间 | 等待窗口 25 秒，轮询式检测 |

## 验证状态

| 项目 | 状态 |
| --- | --- |
| 服务模式连接（MSC 0E0E → 0336 + HASP） | ✅ 实机验证通过（自有 FX30） |
| 二次认证读 HASP 88 字节 | ✅ 实机验证通过 |

离线测试与字节回读不能证明相机在所有情况下都正常，以上验证基于自有单台设备。

## 命令行与开发

| 入口 | 说明 |
| --- | --- |
| `code/fx30_service_probe.py` | 服务模式连接主机侧脚本（macOS，需 root） |

依赖注入：通过 `FX30_DEPS` 环境变量指定 Sony 协议库路径，或将其放在 `deps/` 目录下。

## 许可与独立研究声明

本仓库内容以 **CC BY-NC-SA 4.0**（署名 · 非商业性使用 · 相同方式共享）发布。

- 本项目为**独立研究**，与 Sony 无任何关联，不是 Sony 签名的更新，也不代表 Sony 立场。
- 本仓库不包含固件二进制或受版权保护内容；任何商业性使用、固件分发、绕过安全机制用于牟利的行为一律禁止。
- 如 Sony 或其授权方要求，相关内容将立即下架。

## 支持本项目 ☕

如果本项目对你有所帮助，欢迎自愿支持后续研究（与开源内容无关）：

| 微信 | 支付宝 |
| --- | --- |
| ![微信](assets/wechat-qr.png) | ![支付宝](assets/alipay-qr.png) |
