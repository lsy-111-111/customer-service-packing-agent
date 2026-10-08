# 客服与箱唛 Agent

基于 React、FastAPI 和 SQLite 的本机客服与装箱管理项目。包含商品与客服会话、人工工作台、装箱资料、箱唛 PDF、装箱清单、物流和经营看板。

这是从现有 r11 提取的公开源码版本。下载后会在自己的电脑上建立独立运行环境和虚拟示例数据，**AI 默认关闭**。仓库不包含原使用者的业务数据、密钥、附件、费用账本或历史测试材料。

## 环境要求

| 工具 | 要求 |
| --- | --- |
| Python | 3.11 或 3.12，推荐 3.12 |
| Node.js | 22.13 及以上，推荐 24 LTS |
| pnpm | 10，可在安装 Node.js 后运行 `npm install -g pnpm@10` |
| 系统 | Windows、macOS、Linux；当前实机验证范围见 [发布记录](PUBLICATION.md) |

首次准备环境需要联网下载依赖。Python 和 Node.js 请安装对应系统的版本，并确保命令已加入 PATH。仓库不要求安装 Codex，也不依赖其运行时。

## 下载与启动

可通过 GitHub 的 **Code → Download ZIP** 下载并解压，也可以：

```sh
git clone https://github.com/lsy-111-111/customer-service-packing-agent.git
cd customer-service-packing-agent
```

macOS / Linux：

```sh
python3.12 scripts/run_public.py setup
python3.12 scripts/run_public.py start
```

Windows PowerShell：

```powershell
py -3.12 scripts/run_public.py setup
py -3.12 scripts/run_public.py start
```

使用 Python 3.11 时，把上述 `3.12` 改为 `3.11`。若 `python --version` 已是 3.11 或 3.12，也可以统一使用 `python`。脚本会自行使用本项目的虚拟环境，无需手动激活。

`setup` 安装依赖并构建网页，不启动服务、不调用 AI；以后更新源码或依赖后重新运行它。`start` 在前台运行服务，按 **Ctrl+C** 正常停止，关闭终端也会结束服务；没有后台自启或自动重启。

启动后在浏览器访问：

- 客服与商品：<http://127.0.0.1:8020/>
- 装箱与箱唛：<http://127.0.0.1:8020/packing>
- 物流工作台：<http://127.0.0.1:8020/logistics>
- 经营看板：<http://127.0.0.1:8020/operations-dashboard>

在页面选择演示顾客或店铺客服身份。首次初始化会生成虚拟商品、顾客和示例订单；这不代表真实业务记录。AI 关闭时可浏览页面、使用人工工作台和手工箱唛流程，智能回复会提示转人工，AI 资料提取不会执行。

若端口被占用，可以使用 `start --port 8021`，并相应修改访问地址。入口只监听本机，不会替换已有服务；8001、8017、8018、8019 为保留端口。

## 可选：使用自己的 AI

不需要 AI 时无需创建 `.env`。需要时，在项目根目录复制 `.env.example` 为 `.env`，填写：

```dotenv
AGENT_ENABLED=1
DEEPSEEK_API_KEY=填写你自己的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
MODEL_TIMEOUT=90
MODEL_BUDGET_CNY=填写你自行决定的正数总费用上限
```

以上中文内容是占位说明，需要替换。配置保存后重启服务。密钥和费用上限缺失时，入口会拒绝启用 AI；启动过程本身不发出模型请求。使用 AI 的费用由你自己的服务商账号承担，模型可用性以服务商账户实际情况为准。

公开入口只读取本项目 `.env` 中列出的配置，不继承其他项目的 AI、数据库或费用环境变量。`MODEL_BUDGET_CNY` 使用人民币，最多六位小数，是持续累计的本地费用上限。已有费用和未知预留会保留；同一账本不能通过改小或改大配置重置使用量，也不要删除账本绕过上限。

原代码中的预算预留依赖受支持的模型定价。若自行更换模型或服务地址，需要核实保守单价后，在 `.env` 同时提供 `MODEL_BUDGET_INPUT_CNY_PER_M` 和 `MODEL_BUDGET_OUTPUT_CNY_PER_M`；配置不完整或无法确定保守费用时可能阻止请求。公开发布验证没有发送付费请求，也没有重新验证模型效果或当前价格。

## 数据与文件

| 位置 | 用途 |
| --- | --- |
| `.local/public-runtime/shop.sqlite3` | 本机业务数据库 |
| `.local/public-runtime/uploads/` | 本机上传资料 |
| `.local/public-runtime/model-budget.sqlite3` | 启用 AI 后按需建立的独立费用账本 |
| `.local/releases/` | 带指纹的本机构建产物 |
| `.venv/`、`frontend/node_modules/` | 本机依赖 |

以上目录和 `.env` 均被 Git 忽略。重复启动保留本机数据。备份时先正常停止服务，再复制整个 `.local/public-runtime/`；不要把数据或密钥加入公开仓库。

## 手工箱唛入门

1. 以店铺客服身份进入装箱页面，新建装箱任务。
2. 根据实际资料填写商品、规格、发货数量与每箱件数；例如演示陶瓷杯 25 件、每箱 12 件，计划为 12＋12＋1。
3. 核对收货要求和实际装箱，预览全单 PDF，再按页面流程审核、导出。
4. 使用资料提取前需要显式启用 AI；提取结果仍需人工核对原文。

`frontend/public/templates/` 提供商品和装箱表格模板，`frontend/public/packing-demo/` 为虚构资料。真实箱唛需依据实际收货要求核对；当前没有接入真实支付、ERP/WMS、仓库库存或物流系统。

## 常见问题

- **找不到 Python、Node 或 pnpm**：安装符合上表的版本，重新打开终端后检查 `--version`。
- **安装失败**：确认网络可访问 Python 和 npm 软件包源，再重新运行 `setup`。
- **前端指纹不匹配**：源码或静态资源改变后重新运行 `setup`，不要修改构建记录冒充已通过验证。
- **费用上限与账本不一致**：恢复该账本原有的上限配置，保留费用记录。
- **检测到历史运行配置**：使用独立的干净下载目录，不要复制其他项目的 `.local`、密钥或数据库。

本入口支持跨平台路径与启动方式，但目前仅实测 macOS。发布检查覆盖构建、初始化和页面可访问性；不等于完整业务验收或模型效果验证。详细范围见 [PUBLICATION.md](PUBLICATION.md)。

## 许可

本项目自有代码采用 [MIT](LICENSE) 许可。第三方依赖和资源按各自许可提供，参见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
