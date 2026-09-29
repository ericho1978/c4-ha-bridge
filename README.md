# C4-HA Bridge

Control4 → Home Assistant 的 MQTT 桥接服务。

通过 Control4 控制器内置的 REST API，把灯光、窗帘、空调、继电器同步到 Home Assistant，
使用 MQTT Discovery 自动注册，无需在 HA 里手工配置实体。

**适用**：Control4 OS 2.10.1+（已在 OS 2.10.2 / EA-5 上实测数据结构）
**不依赖 pyControl4**：直接用 aiohttp 调用 Control4 接口，避免第三方库版本与 Python 版本限制。

---

## 一、支持的设备

以下为实测（OS 2.10.2，含第三方驱动）确认的映射：

| 设备 | Control4 proxy | HA 实体 | 状态变量 | 控制命令 |
|------|---------------|---------|---------|---------|
| 灯光（开关型） | `light_v2` | `light` | `LIGHT_STATE` | `ON` / `OFF` |
| 灯光（调光型） | `light_v2` | `light`（带亮度） | `LIGHT_STATE`、`LIGHT_LEVEL` | `ON` / `OFF` / `SET_LEVEL` |
| 窗帘 / 电动窗 | `blind` | `cover` | `Level`、`Open`、`Fully Open`、`Fully Closed`、`Closing` | `SET_LEVEL_TARGET`、`STOP` |
| 空调 / 温控 | `thermostatV2` | `climate` | `TEMPERATURE_C`、`HEAT/COOL_SETPOINT_C`、`HVAC_MODE`、`FAN_MODE` | `SET_MODE_HVAC`、`SET_MODE_FAN`、`SET_SETPOINT_HEAT/COOL` |
| 继电器 / 干接点 | `relaysingle_relay_c4` | `switch` | `RelayState` | `CLOSE`(开) / `OPEN`(关) |

**开关型 / 调光型自动识别**：同一个 `light_v2` proxy 下混着两种驱动
（实测 234 个灯光代理：`**** 开关型` 144 个、`**** 调光型` 82 个、`**** 开关型` 8 个）。
程序不按 proxy 一刀切，而是看设备实际是否返回 `LIGHT_LEVEL`：
首轮状态读取后若发现该变量，就自动升级为调光实体并补发一次 Discovery。
不需要手工配置。

其他查到的 proxy（`keypad_proxy`、`**** 面板`、`media_service`、`projector`、
`amplifier` 等）默认**跳过**；需要扩展时改 `devices.proxies` 配置即可。

> `****_****_Relay`（22 个，名字多为「1」~「19」，另含「折叠门」「新风状态」）：
> 属于 **** 总线侧的继电器，Director REST API 未暴露任何变量或命令，
> 目前**无法通过本桥接控制**（详见「已知限制」）。

---

## 二、工作原理

### 设备识别用 proxy，不用 type

Control4 的 `/api/v1/items` 返回中：

- `type` 只是粗粒度节点类型：1=root、2=site、3=building、4=floor、**6/7=device**、8=room、9=agent
- **`type=7` 才是可控制的功能代理**，`type=6` 是它的硬件父节点（例如「**** 开关型」是父节点，
  「水晶灯」才是可控制的代理）——只取 `type=7` 可避免同一设备重复
- **`proxy` 字段才是准确的设备类型标识**（`light_v2` / `blind` / `thermostatV2` / …）

### 状态读取走批量接口

`/api/v1/items/variables?varnames=A,B,C`（注意没有 item id 段）一次请求即可取回全系统变量值，
返回 `{id, varName, value}` 列表。程序把各设备声明的变量名聚合后一次请求刷新全部状态，
避免数百个设备逐个请求压垮控制器。

若该接口不可用，会依次退化为「按变量逐个批量请求」→「逐设备并发读取」，并在日志中说明。

### 空调模式动态映射

空调的模式列表来自设备本身（如 `Off,On,Heat,Cool,Dehumidify,通风,Auto`，含中文项），
程序在读到 `HVAC_MODES_LIST` / `FAN_MODES_LIST` 后动态生成 HA 的 `modes` / `fan_modes`
并重发一次 Discovery，因此不会写死。

---

## 三、部署

### 前置条件

- Control4 控制器局域网可达（HTTPS 443，自签名证书）
- HA 已装 MQTT Broker（Mosquitto），桥接器与 Broker 网络互通
- 能访问 `apis.control4.com`（账户认证用；若配了有效 `director_token` 可不需要）

> 无需在 Composer 里开启任何 API：Director 的 `/api/v1/...` 在 OS 2.10.1+ 默认可用，
> 认证通过 Control4 云账号换取本地 token。

### 方式 A：直接 Python 运行

```bash
cd /path/to/c4-ha-bridge
pip3 install -r requirements.txt      # 仅 aiohttp / gmqtt / PyYAML

python3 test_c4_api.py                 # 1) 验证连接与认证
python3 test_c4_control.py             # 2) 只读：发现设备 + 校验批量接口 + 状态解析
python3 app/main.py                    # 3) 正式运行
```

长期运行建议交给 systemd：

```ini
# /etc/systemd/system/c4-ha-bridge.service
[Unit]
Description=C4-HA Bridge
After=network-online.target

[Service]
WorkingDirectory=/home/ha/c4-ha-bridge
ExecStart=/usr/bin/python3 app/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now c4-ha-bridge
journalctl -u c4-ha-bridge -f
```

### 方式 B：Docker

```bash
docker-compose up -d --build
docker logs -f c4-ha-bridge
```

> `docker-compose.yml` 的 `version` 需与本机 compose 版本匹配：
> compose 1.25 及以下最高支持 `"3.7"`，报 `Version ... is unsupported` 就改成 `"3.7"`。

### 在 HA 中确认

启动后实体自动出现在 **设置 → 设备与服务 → MQTT**。
设备数较多（本例 511 个）时 HA 首次注册需要一会儿，实体按房间归入 Area。

---

## 四、配置说明

见 `config.example.yaml`。要点：

| 配置项 | 说明 |
|--------|------|
| `control4.ip` | 控制器局域网 IP |
| `control4.director_token` | 可选，跳过云端认证；**约 24h 过期，长期运行建议留空** |
| `control4.verify_director_token` | token 无效时自动回退账户认证（默认 true） |
| `control4.username/password` | 账户认证，需要能登录 Control4 账户 |
| `mqtt.*` | Broker 地址与认证 |
| `devices.proxies` | proxy → 组件类型映射，扩展新设备类型时改这里 |
| `devices.disabled_components` | 整类禁用，如 `["switch"]` 关掉全部继电器 |
| `devices.include_ids` / `exclude_ids` | 按 item id 白/黑名单 |
| `polling.interval` | 轮询间隔秒数，设备多时建议 30-60 |
| `polling.use_batch` | 批量变量读取（强烈建议保持 true） |

### MQTT 主题

```
c4-ha/bridge/state                        online / offline

c4-ha/light/c4_472/state                  {"state":"ON"}
c4-ha/light/c4_472/set                    ON / OFF

c4-ha/light/c4_819/state                  {"state":"ON","brightness":42}   ← 调光型
c4-ha/light/c4_819/brightness/set         0-100

c4-ha/cover/c4_493/state                  {"state":"open","position":0}
c4-ha/cover/c4_493/set                    OPEN / CLOSE / STOP

c4-ha/climate/c4_26/state                 {"mode":"cool","temperature":26,...}
c4-ha/climate/c4_26/mode/set              off/heat/cool/auto/dry/fan_only
c4-ha/climate/c4_26/temperature/set       26
c4-ha/climate/c4_26/fan_mode/set          low / medium / high / auto

c4-ha/switch/c4_312/state                 {"state":"OFF"}
c4-ha/switch/c4_312/set                   ON / OFF
```

亮度统一使用 0-100（Discovery 里 `brightness_scale: 100`），与 Control4 的
`LIGHT_LEVEL` 一致，不做换算。

---

## 五、现场校验

改动或新增设备后，按顺序跑：

```bash
python3 test_c4_api.py          # 认证与连接
python3 test_c4_control.py      # 只读：设备发现 + 批量接口 + 状态解析
python3 test_c4_probe3.py       # 需要时：普查某类设备的全部变量与命令
```

**控制实测**（会真的动作设备，默认关闭）：

```bash
python3 test_c4_control.py --apply
python3 test_c4_control.py --apply --light 472 --blind 493 --thermostat 26
```

`--apply` 会开关一次灯、开合一次窗帘、改一次空调模式和温度，并尽量恢复原状态；
继电器只读不动作（避免影响供电设备）。

窗帘的命令写法有两种可能，程序会自动适配：先用
`command=SET_LEVEL_TARGET` + `tParams={"LEVEL_TARGET":...}`，被拒绝则改用
`command="SET_LEVEL_TARGET:LEVEL_TARGET_OPEN"`，并记住可用写法。首次下发时可在日志看到
「使用命令写法 A/B」。用 `--apply` 可现场确认你的控制器接受哪一种。

---

## 六、已知限制

- **`****_****_Relay` 的 22 路继电器无法控制**：这些条目的 `/variables` 与
  `/commands` 均为空列表，`capabilities` 为空；只有 `/properties` 里有一个
  `Relay Status`（OPENED/CLOSED）属性。它们通过 bindings 挂在 **** 网关
  （**** 网关，****:6000）下，属于总线侧设备。当前 REST API 没有可用的
  控制入口，因此未桥接。如需控制，只能从 **** 面板 / **** App 侧操作。
- **窗帘不支持任意百分比定位**：驱动只提供开/关/停，因此 HA 只暴露开合停，不提供位置滑块。
- **状态非实时**：采用轮询（默认 30s），HA 中状态变化有延迟；不支持 WebSocket 推送。
- **账户认证需访问云接口**：需要能访问 `apis.control4.com`。
- **不支持的设备**：面板按键、影音设备、以及无 API 能力的驱动未桥接。
- **部分「灯」实为虚拟开关**：如 `离开模式`、`唱歌`、`电影` 等也是 `light_v2` 形态的代理，
  会以灯的形式出现。如需隐藏可用 `devices.exclude_ids`。

---

## 七、故障排查

| 现象 | 排查 |
|------|------|
| 启动报未发现设备 | 看日志里 `type=7 的功能代理设备 N 个` 与「未支持的 proxy 类型」列表，按需补 `devices.proxies` |
| 批量读取不可用 | 日志会提示退化方式；可把 `polling.interval` 调大、或 `use_batch: false` 观察差异 |
| 状态一直是初始值 | 确认 `polling.enabled: true`，并检查是否所有设备都读到了变量（DEBUG 日志有统计） |
| 下发命令无效果 | 用 `test_c4_control.py --apply` 看命令回读；确认命令名与参数是否匹配该驱动 |
| 灯具全不可用 | 检查 MQTT Broker 与 `c4-ha/bridge/state` 是否为 `online` |
| HA 实体过多卡顿 | 用 `devices.disabled_components` / `include_ids` 精简 |

---

## 八、目录结构

```
c4-ha-bridge/
├── app/
│   ├── main.py                入口
│   ├── config.py              配置加载
│   ├── c4_compat.py           Control4 云端认证 + Director REST 客户端
│   ├── mqtt_client.py         MQTT 客户端
│   ├── bridge.py              发现 / 批量读取 / 命令路由
│   └── devices/
│       ├── base.py            设备基类（Discovery、状态、命令）
│       ├── registry.py        proxy → 设备类映射
│       ├── light.py           灯光
│       ├── blind.py           窗帘
│       ├── thermostat.py      空调
│       └── relay.py           继电器
├── config/config.yaml         实际配置（含密码，勿提交）
├── config.example.yaml        配置模板
├── test_c4_api.py             连通性测试
├── test_c4_control.py         现场校验（默认只读，--apply 实测控制）
├── test_c4_probe3.py          设备能力普查（新增设备类型时用）
├── test_c4_probe5.py          **** 继电器控制入口探测
├── test_c4_probe6.py          继电器控制通道 + 调光能力实测
├── Dockerfile / docker-compose.yml
└── requirements.txt
```
