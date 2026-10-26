# FX30 固件研究项目（FX30 Firmware Research Project）

> Sony FX30（ILME-FX30）固件纯学术研究记录。当前公开：**服务模式连接全链路**、**隐藏高速连拍开启/回滚**（均含可运行代码）。其余研究阶段暂不公开。

## 支持机型

> **本项目仅针对 Sony FX30（ILME-FX30）。** 固件逆向与设备修改存在**变砖风险**，可能使设备失去保修。所有操作**风险自负**，请仅在自有设备上、在理解全部风险的前提下进行。出错时请先停止，不要盲目重试写入。

## Contents

- [重要声明](#重要声明)
- [已公开内容](#已公开内容)
- [环境与依赖](#环境与依赖)
- [开始前注意](#开始前注意)
- [风险分级](#风险分级)
- [服务模式连接：分步指南](#服务模式连接分步指南)
- [开启隐藏高速连拍](#开启隐藏高速连拍)
- [故障排查](#故障排查)
- [验证状态](#验证状态)
- [命令行与开发](#命令行与开发)
- [许可与独立研究声明](#许可与独立研究声明)

---

## 重要声明

- 本项目**仅供学术研究与技术学习**，**不用于任何商业行为**，禁止用于盈利、侵权或非法用途。
- 本仓库**不包含任何 Sony 固件二进制、加密密钥或受版权保护内容**，仅包含研究方法、流程记录与主机侧代码。
- 本项目与 Sony Corporation **无任何关联**，不是 Sony 签名的更新，也不代表 Sony 立场。
- 请尊重 Sony 知识产权与用户协议；如 Sony 或其授权方要求，本项目相关内容将立即下架。
- 所有地址、偏移、命令号均为**逆向分析结果，非官方文档**，可能存在误判，请以你自己的实机验证为准。

---

## 已公开内容

| 功能 | 说明 | 代码 |
| --- | --- | --- |
| **服务模式连接** | 通过 USB 将 FX30 从标准 MSC 存储设备身份（`054C:0E0E`）切换为 Sony 服务模式（`054C:0336`），完成 Senser 认证并读取 HASP 服务令牌（88 字节）——后续一切只读取证与固件研究的地基 | [`code/fx30_service_probe.py`](code/fx30_service_probe.py) |
| **隐藏高速连拍开启 / 回滚** | 在设置容器（BK4）里**成对**写入两个字节并重算校验和，断电冷启动后相机进入连拍模式、可连续拍摄；一条命令回滚 | [`code/fx30_burst.py`](code/fx30_burst.py) |

---

## 环境与依赖

| 项 | 说明 |
| --- | --- |
| 主机 | macOS（脚本含 `ioreg` / `diskutil` 卸载逻辑，可适配其他平台） |
| 运行权限 | Python 3，需 `sudo` 运行以访问 USB 层 |
| 依赖 | `pyusb` + `libusb_package`，以及 Sony 相机 USB 协议封装库（`pmca` / `transport`，第三方开源库，不在本仓库内分发，请按上游说明安装） |

```bash
# 服务模式连接（只读）
sudo PYTHONPATH="<协议库路径>:<依赖路径>" python3 code/fx30_service_probe.py

# 连拍工具（FX30_DEPS 指向协议库的 camera_runtime 目录）
export FX30_DEPS=/path/to/a7c2-fl23-research/src/camera_runtime
sudo -E PYTHONPATH="$FX30_DEPS:$FX30_DEPS/vendor" python3 code/fx30_burst.py --probe
```

## 开始前注意

- 相机电量不低于 50%；**写操作期间不要拔线、不要让电脑休眠**。
- 连接过程相机可能显示「请检查连接的设备」、电脑可能不出现盘符——均为正常现象，以脚本日志为准。
- 仅当相机以 **Mass Storage（存储）** 模式接入时执行。
- 做**写操作**前，先跑一次 `--probe` 落快照；没有快照就不要写。
- 写后必须**拔 USB → 取电池 ≥30 秒 → 装回开机**（设置区是 warm-boot 镜像，热重启会还原）。

## 风险分级

| 操作 | 风险 | 可回滚 | 说明 |
| --- | --- | --- | --- |
| `--probe`（只读、落快照） | 零 | — | 只发读命令，不写任何字节 |
| 设置容器写（`--burst` / `--single`） | 低–中 | ✅ 快照 + 自动回滚 | 只写 2.25MB 设置容器，不碰固件分区；写后读回校验，不符合预期自动写回快照 |
| 写固件分区（`/dev/nflasha*`、`/usr` 资源） | **高** | ❌ 本仓库不提供 | **写坏可能不开机**，需要硬件级恢复预案；本项目不做，也不建议 |

> 已知危险命令（不要碰）：`0x4003`(FORMAT)、`0x4006`(FIRMUP)、`0x60F` 区间、以及任何整区写入。

---

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

---

## 开启隐藏高速连拍

> ⚠️ **会写相机设置区**。先读上面的[风险分级](#风险分级)，并在 `--probe` 阶段确认快照已生成。

### 效果

- 相机进入 **连拍（BURST / CONT_SHOOTING）** 驱动模式，屏幕显示「连拍」，可以连续拍摄。
- **限制**：「连拍」不会出现在菜单的「拍摄模式」选项列表里。手动去切那个控件，会从原本的选项重新取值、掉回单张。
  这是固件层的行为，不是本工具能解决的，也不是本项目的目标。

### 写入内容

| 偏移 | 旧 → 新 | 含义 |
| --- | --- | --- |
| `0x08EF80` | `0x00 → 0x01` | `EXTERN_VAL_DRIVE_MODE_*`（属性 `0x02cf013b`）的枚举位置 |
| `0x091690` | `0x00 → 0x01` | `DRIVE_MODE_*`（属性 `0x02cf02ae`）的枚举位置 |

- 这两个字节**不是属性 id**，是两张枚举表里的 0-based 位置索引。
- **必须成对写**：只写一个会被固件回退。
- 写入前会重算 bank0 校验和（`bank+0x20 = sum(bank[:0x20]) + sum(bank[0x24:])`），校验不自洽就不写。

### 三步

```bash
# ① 只读：读回 2.25MB 容器、校验两个 bank、落快照、打印当前档位
sudo -E PYTHONPATH="$FX30_DEPS:$FX30_DEPS/vendor" python3 code/fx30_burst.py --probe

# ② 演练：算补丁 + 重算校验和，不写
sudo -E PYTHONPATH="$FX30_DEPS:$FX30_DEPS/vendor" python3 code/fx30_burst.py --burst --dry-run

# ③ 实写 → 拔 USB → 取电池 ≥30 秒 → 装回开机
sudo -E PYTHONPATH="$FX30_DEPS:$FX30_DEPS/vendor" python3 code/fx30_burst.py --burst --yes
```

### 回滚

```bash
sudo -E PYTHONPATH="$FX30_DEPS:$FX30_DEPS/vendor" python3 code/fx30_burst.py --single --yes
# 或者写回快照：
sudo -E PYTHONPATH="$FX30_DEPS:$FX30_DEPS/vendor" python3 code/fx30_burst.py --restore fx30_dump/bk4_snapshot_<时间戳>.bin
```

回滚后同样要**断电冷启动**一次。

### 工具做了什么

1. 读回整个设置容器，落快照到 `./fx30_dump/`，本地校验 bank 校验和；
2. 只改白名单里的两个偏移（白名单之外的偏移、或单次超过 8 字节，直接拒绝）；
3. 重算受影响 bank 的校验和并二次校验；
4. 写入 → save → 读回验证目标字节；读回不符合预期时，**自动写回写前快照**；
5. 全程落日志 `fx30_dump/session_<时间戳>.log`。

### 实测状态

| 项 | 状态 |
| --- | --- |
| 写入被接受（`res=0`） | ✅ 实机多次 |
| 断电冷启动后进入连拍 | ✅ 实机多次 |
| 连续拍摄 | ✅ 实机验证 |
| 回滚到单张 | ✅ 实机验证 |
| 连拍出现在菜单列表 | ❌ 未达成（固件层限制，见「效果」） |

---

## 故障排查

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| MSC 查询后认证失败 | 同一 USB 句柄先做 SCSI 再发认证，bulk 端点状态被污染 | 查询与认证**必须用不同句柄**；查询句柄用完即 dispose |
| macOS 上认不出设备 | 内核驱动占用接口 | 必须先 `detach_kernel_driver(0)` |
| 卸载报错 | 卸载了单个卷而非整个磁盘 | 用 `diskutil unmountDisk` |
| 认证后等不到 0336 | 重枚举需要时间 | 等待窗口 25 秒，轮询式检测 |
| 写入返回 `res=129` | 固件拒绝（校验和/内容不自洽） | 先跑 `--dry-run` 看校验和是否自洽；不要反复重试写入 |
| 写后读回目标字节没变 | 被固件归一/回写 | 工具会自动回滚；确认写的是 `0x8EF80`/`0x91690` 这两个偏移 |
| 重启后回到单张 | 没有断电冷启动 | 拔 USB、取电池 ≥30 秒再开机 |
| 开机异常 | 设置区可能半写 | **停止写入**，用 `--restore` 写回快照 |

---

## 验证状态

| 项目 | 状态 |
| --- | --- |
| 服务模式连接（MSC 0E0E → 0336 + HASP） | ✅ 实机验证通过（自有 FX30） |
| 二次认证读 HASP 88 字节 | ✅ 实机验证通过 |
| 连拍写入 → 断电 → 进入连拍 | ✅ 实机验证通过（2026-10-26，多次） |
| 连拍回滚 | ✅ 实机验证通过 |
| 连拍进入菜单列表 | ❌ 未达成 |

离线测试与字节回读不能证明相机在所有情况下都正常，以上验证基于自有单台设备。

## 命令行与开发

| 入口 | 说明 |
| --- | --- |
| `code/fx30_service_probe.py` | 服务模式连接主机侧脚本（macOS，需 root） |
| `code/fx30_burst.py` | 连拍开启 / 回滚 / 快照恢复（`--probe` / `--burst` / `--single` / `--dry-run` / `--restore`） |

依赖注入：通过 `FX30_DEPS` 环境变量指定 Sony 协议库路径，或将其放在 `deps/` 目录下。

## 许可与独立研究声明

本仓库内容以 **CC BY-NC-SA 4.0**（署名 · 非商业性使用 · 相同方式共享）发布。

- 本项目为**独立研究**，与 Sony 无任何关联，不是 Sony 签名的更新，也不代表 Sony 立场。
- 本仓库不包含固件二进制或受版权保护内容；任何商业性使用、固件分发、绕过安全机制用于牟利的行为一律禁止。
- 如 Sony 或其授权方要求，相关内容将立即下架。

<details>
<summary>支持本项目 ☕</summary>

如果本项目对你有所帮助，欢迎自愿支持后续研究（与开源内容无关）：

| 微信 | 支付宝 |
| --- | --- |
| ![微信](assets/wechat-qr.png) | ![支付宝](assets/alipay-qr.png) |

</details>
