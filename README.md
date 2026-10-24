# mercari-watch　煤炉关键字监控

盯住 Mercari（煤炉）某个关键字，出现下面这些变化就发邮件给你：

- **新上架**：关键字命中、并且过了你设的价格/排除词筛选的新商品
- **降价**：降幅达到你设的百分比或金额
- **跌破目标价**：价格第一次低于你心里的那个数（只报一次，不刷屏）
- **售出 / 下架**：在售商品被买走或被删除

纯 Python 标准库实现，**不需要 pip 安装任何东西**，Windows 上开箱即用。

---

## 0. 先说硬前提：网络

Mercari 不在国内可直连的范围内。实测（`probe.bat`）三层结果：

| 层次 | 结果 |
| --- | --- |
| 系统 DNS | `104.244.43.248`、`108.160.169.175` —— 分别属于 Twitter 和 Dropbox，典型 DNS 污染 |
| DoH（腾讯） | `104.18.37.23`、`172.64.150.233` —— 真实地址（Cloudflare） |
| 用真实 IP 建 TLS | `ConnectionResetError` —— 握手被重置 |

结论：**DNS 污染能绕，IP 层的拦截绕不过**。本机直连不行，抓取必须从海外出口发起。

好消息是你选了邮件通知，抓取和收信是分开的：

```
海外出口抓煤炉 ──SMTP──▶ 你的邮箱 ──▶ 手机/电脑（国内直连，不需要任何代理）
```

四条路：

| 方案 | 要装 VPN 吗 | 费用 | 轮询间隔 | 说明 |
| --- | --- | --- | --- | --- |
| **日本 HTTP 代理（推荐）** | 不用 | ¥10–30/月 | 30–60 秒 | 填一行地址，本机照常跑 |
| 日本 VPS | 不用 | ¥25–40/月 | 30 秒 | 最稳，但要维护一台机器 |
| GitHub Actions | 不用 | 免费 | 5–10 分钟 | 零成本，抢热门货来不及 |
| 本机 + 自己的代理软件 | 要 | 你现有的 | 30 秒 | 有的话最省事 |

⚠️ **阿里云/腾讯云的国内机器不行**，出口还在国内，一样被拦。

### 怎么买代理

要买的是 **HTTP 代理**（不是 VPN 客户端），选**日本节点**、按流量计费。国内几家按量代理（快代理、芝麻、青果这类）都有日本线路，注册后会给你一条这样的地址：

```
http://用户名:密码@gate.xxx.com:12345
```

别急着付钱——先拿试用流量测。拿到地址跑：

```bat
python -m mercari_watch proxy-test --proxy "http://用户名:密码@gate.xxx.com:12345"
```

它依次检查：地址格式 → 代理端口 → 出口 IP 归属国 → **真的用它拉一次煤炉搜索页并解析商品**。
只有最后一步过了才算可用。很多"日区代理"出口其实在别处，页面能开但一个商品都解析不出来。

测通后写进 `config.toml`：

```toml
[network]
proxy = "http://用户名:密码@gate.xxx.com:12345"
```

`doctor` / `once` / `run` 也都支持 `--proxy` 临时指定，不用改文件就能试。

### 流量大概多少

一轮 ≈ 一个搜索页（gzip 后 60–100 KB）：

| 轮询间隔 | 每月流量 | 按 ¥1/GB 估 |
| --- | --- | --- |
| 60 秒 | 约 3–5 GB | ¥3–5 |
| 120 秒 | 约 1.5–2.5 GB | ¥2–3 |
| 300 秒 | 约 0.7–1 GB | ¥1 |

所以「10 分钟内会被抢」这种，**60 秒轮询 + 最便宜的按量代理**就够，一个月几块钱。

---

## 1. 三步跑起来

```bat
cd /d D:\AIProject\mercari-watch

:: ① 生成配置（只做一次）
python -m mercari_watch init

:: ② 改 config.toml：关键字、价格、邮箱；有代理就填 network.proxy
notepad config.toml

:: ③ 自检 —— 这一步会明确告诉你卡在哪
check.bat
```

`doctor` 全部 OK 之后：

```bat
once.bat          :: 先跑一轮，看看抓到了什么、判定对不对
run.bat           :: 常驻监控（关窗口就停）
```

第一次 `once` 是**建基线**：它会把当前所有在售商品记进数据库，但**不逐条报老货**（除非你把 `alert_on_first_run` 改成 `true`）。从第二轮开始，只有真正的新变化才会发信。

---

## 2. 配置怎么写

每个 `[[watch]]` 就是一个监控项，用 `mode` 区分两类：

| | `mode = "new"`（L1） | `mode = "price"`（L2） |
| --- | --- | --- |
| 干什么 | 搜关键字，**发现新商品就报** | 搜关键字**锁定一件**，之后只盯它 |
| 判定 | 出现没见过的 item_id | 价格变了 / 售出 / 下架 |
| 每轮请求 | 1 页搜索结果 | 1 个商品页 |
| 典型用途 | 蹲某类货谁上新了 | 盯准一台，等它降价 |

L2 的锁定规则：**首次运行搜索一次，取第一条符合筛选条件的商品锁住**（所以关键字要写全，越准锁得越准）。
锁错了改 `keyword` 再 `relock`；目标被买走想换一件，也用它：

```bat
python -m mercari_watch relock "盯这台 Switch"
```

```toml
# ---------- L1：找新货 ----------
[[watch]]
name = "Switch OLED 新货"
mode = "new"
keyword = "Nintendo Switch 有機EL"
interval = 300                    # 每 5 分钟（最小 30 秒）
pages = 2                         # 结果 50 条以内建议 2；一页约 30 条
sort = "created_time"             # 按发布时间从新到旧（固定这么排）

[watch.filters]
price_min = 15000
price_max = 40000
exclude_keywords = ["ジャンク", "訳あり"]   # 排除故障品/有瑕疵

[watch.rules]
new_item = true                   # 有新增就报
price_drop = false                # L1 不关心价格
status_change = false             # 也不复查状态，省请求

# ---------- L2：盯一件的价格 ----------
[[watch]]
name = "盯这台 Switch"
mode = "price"
keyword = "Nintendo Switch 有機EL ホワイト"
interval = 300
pages = 1

[watch.rules]
new_item = false
price_drop = true
price_increase = true             # 涨跌都报
price_drop_percent = 0            # 0 = 任意降价都报
below_price = 26000               # 跌破 2.6 万单独提醒一次
status_change = true              # 售出 / 下架也报
```

### 规则字段速查

| 字段 | 作用 |
| --- | --- |
| `mode` | `new` = L1 找新货；`price` = L2 盯一件 |
| `new_item` | 新上架要不要报 |
| `price_drop` | 降价要不要报 |
| `price_increase` | **涨价**要不要报（L2 常用；默认关） |
| `price_drop_percent` / `price_drop_absolute` | 降价门槛：百分比 **或** 金额达到任一即报；两个都写 0 就是任意降价都报 |
| `below_price` | 跌破这个价格报一次（之后不重复），0 = 关闭 |
| `status_change` | 售出 / 下架报不报 |
| `alert_on_first_run` | 首次建基线时，要不要把现存商品也全报一遍 |
| `exclude_keywords` | 标题里含这些词就跳过（"ジャンク"= 故障品，"訳あり"= 有瑕疵） |
| `require_keywords` | 标题必须含这些词才算命中 |
| `price_min` / `price_max` | 价格区间，0 = 不限 |

### 几处容易踩的行为细节

- **L1 建基线要翻完所有页**，否则第二页的老货会被当成"新货"全报一遍。稳态下每轮只看第 1 页。
- **被筛选条件挡掉的商品也会记进库**（只是不通知），否则每轮都会重新判定它是不是"新货"。
- **L1 和 L2 可以盯同一件商品**，两边各自记价格基准、互不干扰。
- **L2 锁定后不再搜索**，只请求商品页；所以搜索结果是 30 条还是 100 条都不影响它。
- **L2 目标售出后会报一次**，之后不再重复请求，状态停在"已售出"。

### 邮箱怎么拿授权码

| 邮箱 | SMTP | 端口 | security |
| --- | --- | --- | --- |
| QQ | `smtp.qq.com` | 465 / 587 | `ssl` / `starttls` |
| 163 | `smtp.163.com` | 465 | `ssl` |
| Gmail | `smtp.gmail.com` | 465 | `ssl`（国内网络不通，且要用应用专用密码） |
| Outlook | `smtp.office365.com` | 587 | `starttls` |

QQ 邮箱：设置 → 账户 → 开启 SMTP 服务 → 生成**授权码**（16 位），填到 `password`。**不是登录密码。**

不想把授权码写进文件，可以改用环境变量：

```bat
set MERCARI_WATCH_SMTP_PASSWORD=你的授权码
```

配置好后先单独测一封：

```bat
python -m mercari_watch mail-test
```

---

## 3. 让它一直跑（Windows）

### 方式 A：计划任务（推荐，开机自动起，无窗口）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
```

它会注册一个名为 `MercariWatch` 的任务，登录时自动启动，用的是 `pythonw.exe`（没有黑窗口），崩溃了 1 分钟后自动重启。

```powershell
# 看运行情况
Get-ScheduledTask -TaskName MercariWatch | Get-ScheduledTaskInfo

# 卸载
powershell -ExecutionPolicy Bypass -File scripts\uninstall_task.ps1
```

### 方式 B：直接双击 `run.bat`

简单粗暴，缺点是窗口不能关、重启后要手动再点。

### 看日志

```
data\watch.log          实时日志（自动轮转，最多 5 份 × 2MB）
python -m mercari_watch status    看每个关键字的进度和最近事件
```

---

## 4. 抓取是怎么做的（以及为什么可能失效）

Mercari 没有公开 API，程序走两条通道，`fetch_mode = "auto"` 时自动择优：

1. **网页通道**（默认首选）：请求 `jp.mercari.com/search`，从页面内嵌的 JSON / RSC 数据流里解析商品。不需要签名，最不容易被拦。
2. **接口通道**：POST `api.mercari.jp/v2/entities:search`，需要 DPoP 签名。签名用纯 Python 实现的 P-256 ECDSA（`mercari_watch/crypto.py`），不依赖 `cryptography`。

**要注意的现实：**

- 网页和接口结构都可能改。真改了，`doctor` 会显示「页面拿到了，但一个商品都没解析出来」。
  这时加 `--dump` 存下原始页面，`data\dumps\` 里就是现场，改 `fetcher.py` 里的解析规则即可。
- 也可以离线验证解析器：
  ```bat
  python -m mercari_watch parse data\dumps\doctor_xxxx.html
  ```
- 请求别太猛。`interval` 最小 30 秒，`pages` 别超过 2-3，否则容易触发风控（返回 429 或空列表）。
- 这是**非官方**手段，Mercari 有权随时调整。程序做了重试和退避，但没法保证永久可用。
- 本工具只读公开的在售列表，不登录、不抢购、不自动下单。

---

## 5. 排错

| 现象 | 原因 / 处理 |
| --- | --- |
| `doctor` 报 DNS 解析到 `199.59.150.x` | DNS 污染，必须挂代理 |
| `doctor` 报 TLS 超时、连接被重置 | 网络被拦，挂代理；或换 VPS |
| 页面抓到了但解析出 0 个商品 | 页面结构变了，`--dump` 存页面后改解析规则 |
| 收不到邮件 | 先 `mail-test`；检查授权码、端口/security 是否配对；看垃圾箱 |
| 一直报同一件商品 | 检查是不是 `below_price` 场景 —— 它只报一次；降价按 `price_drop_percent` 判定 |
| 首次运行就刷了一堆邮件 | `alert_on_first_run` 被设成 `true` 了，改回 `false` |
| 计划任务跑着但没数据 | 看 `data\watch.log`；多半是任务里的工作目录不对 |

---

## 6. 零成本方案：GitHub Actions（不用代理、不用服务器）

如果 5 分钟粒度够用，这条路**完全免费**：GitHub 的机器在海外，直接就能抓煤炉，你本地什么都不用装。

### 先看清楚代价

- **不是精确的 5 分钟**。GitHub 的 cron 在高负载时会延迟，实际间隔常是 5–15 分钟。排在热门商品上可能输给更快的人。
- **仓库必须公开**。私有仓库每月只有 2000 分钟，这个频率会超（每轮约 1 分钟 × 288 轮/天）。
- 邮箱密码不能写进文件，要走 Secrets。
- 公开仓库 60 天没有 commit 推送，GitHub 会自动暂停定时任务（推一次就行）。

### 步骤

**① 建仓库并推送**（把 `mercari-watch` 目录本身作为仓库根目录）

这台机器还没装 git，先装一下（`winget` 是系统自带的）：

```bat
winget install --id Git.Git -e --source winget
```

装完**新开一个命令行窗口**（PATH 才会生效），然后：

```bat
cd /d D:\AIProject\mercari-watch
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin https://github.com/你的账号/mercari-watch.git
git push -u origin main
```

> 不想用命令行的话，装 [GitHub Desktop](https://desktop.github.com/) 图形界面也能推，效果一样。

> `config.toml` 和 `data/` 已被 `.gitignore` 排除，不会传上去。

**② 加 Secrets**：仓库 → Settings → Secrets and variables → Actions → New repository secret，只需要加 **3 个**：

| 名字 | 值 |
| --- | --- |
| `SMTP_USERNAME` | `you@qq.com` |
| `SMTP_PASSWORD` | 邮箱授权码 |
| `SMTP_TO` | `you@qq.com`（多个用逗号分隔） |

服务器地址、端口、加密方式不敏感，已经写死在 `config.ci.toml` 里（`smtp.qq.com` / 465 / ssl），换邮箱服务商时改那个文件。

凭据只存在于 Secrets 里，代码读的是环境变量（见 `_apply_email_env`），公开仓库看不到你的邮箱和授权码。

**③ 改关键字**：编辑仓库里的 `config.ci.toml`（这个文件是给 Actions 用的），改 `keyword`、价格区间、`below_price`，然后提交：```bat
notepad config.ci.toml
git add config.ci.toml
git commit -m "set keyword"
git push
```

**④ 验证**：仓库 → Actions → 左边选 `mercari-watch` → 右边 `Run workflow` 手动跑一次。
第一轮是建基线（不发信），日志里应看到 `第 1 页：N 条`。之后再手动跑一次，如果这期间有新上架，邮件就来了。

### 怎么确认它真的在跑

- Actions 页面每轮都有日志
- `恢复状态` 那步命中缓存会显示 `Cache restored`
- 连续失败会发 GitHub 通知；真被煤炉拦了就这样，去 Actions 页面 Disable workflow 停掉

### 和代理方案怎么选

| | GitHub Actions | 日本代理 + 本机 |
| --- | --- | --- |
| 费用 | 0 | ¥1–5/月流量 |
| 实际间隔 | 5–15 分钟 | 你设的 60–300 秒 |
| 稳定性 | 依赖 GitHub 调度 | 依赖代理线路 |
| 维护 | 只改配置文件 | 本机常开 / 计划任务 |

建议**先跑 Actions**（零成本、立刻能用），发现总是慢半拍再上代理。两个别同时开，同一件商品会收到两份邮件。

---

## 7. 目录结构

```
mercari-watch/
├─ mercari_watch/
│  ├─ __main__.py     命令行入口（init/doctor/proxy-test/once/run/status/mail-test/parse）
│  ├─ config.py       TOML 配置加载与校验
│  ├─ fetcher.py      两条抓取通道 + 页面解析
│  ├─ crypto.py       ES256 签名 / DPoP 令牌
│  ├─ rules.py        变化判定（纯函数，好测）
│  ├─ store.py        SQLite 状态库
│  ├─ notify.py       SMTP 邮件（HTML 摘要）
│  ├─ proxytest.py    代理体检：出口国 + 真拉一次煤炉
│  ├─ runner.py       调度主循环
│  ├─ doctor.py       环境自检
│  └─ http.py         带代理/重试的 HTTP 客户端
├─ tools/probe_network.py   不用 VPN 能不能直连的探测脚本
├─ .github/workflows/watch.yml   GitHub Actions 定时任务
├─ config.ci.toml     Actions 用的配置（关键字写这里，凭据走 Secrets）
├─ tests/             87 个单元测试
├─ scripts/           计划任务安装/卸载
├─ config.example.toml
├─ config.toml        你的配置（init 生成，已 gitignore）
├─ run.bat / once.bat / check.bat / probe.bat
└─ data/              数据库、日志、DPoP 密钥、页面快照（已 gitignore）
```

跑测试：

```bat
python -m unittest discover -s tests -t . -v
```
