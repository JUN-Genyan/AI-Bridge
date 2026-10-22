# AI Bridge

一个本地网页"会议室"：你、Claude Code、Codex 三方在同一个聊天里开会，两个 AI 各用你本机已经登录好的 CLI（`claude`、`codex`），看得到彼此说的话，可以互相 @、分工、一起改同一个项目。几场会议可以同时开、同时跑；文件护栏保证两个 AI 不会同时改同一个文件；会议室自己带一个 MCP 服务，网页能装成 app。

![icon](roundtable_preview.png)

## 要什么

- Windows（主要在 Windows 11 上用；codex 沙箱那几条是 Windows 专属的处理，别的系统没测过）。
- Python 3.11+（只用标准库，不用装包）。
- [Claude Code](https://docs.claude.com/en/docs/claude-code) 的 `claude` 命令、[Codex CLI](https://github.com/openai/codex) 的 `codex` 命令，都已登录，在 PATH 里。
- 工作文件夹最好是 git 仓库：干活模式靠 git 记每一步改了什么、能撤回；不是 git 仓库也能用，只是撤不回。

## 怎么开

- 双击 `start.bat`（带黑窗口，关掉就停），或者双击 `open.pyw`（后台开，不弹窗，没开就开、开了就直接打开网页）。地址 http://127.0.0.1:8765 。也可以 `python roundtable.py` 或 `python -m bridge`。
- 第一次打开会让你开一场会议：标题、目标、工作文件夹（两个 AI 在哪个文件夹里干活）。
- 会议记录、附件都存在本文件夹的 `data\`（不进 git）。
- 装成 app：电脑上 Chrome / Edge 地址栏右边的"安装"图标；手机经 `tailscale serve` 的 https 地址打开后"添加到主屏幕"。见"装成 app"一节。

可选的环境变量：

| 变量 | 作用 | 默认 |
|---|---|---|
| `ROUNDTABLE_PORT` | 端口 | 8765 |
| `ROUNDTABLE_LOG_DIR` | 会议记录放哪 | 本文件夹的 `data\` |
| `ROUNDTABLE_DEFAULT_DIR` | 新会议默认的工作文件夹（这个标签页没开着会议、也没用过别的文件夹时） | 你的用户目录 |
| `ROUNDTABLE_NO_BROWSER` | 设成 1 启动时不自动开浏览器 | 不设 |
| `ROUNDTABLE_CODEX` | 设成 `exec` 退回旧的每轮一个 `codex exec` | `appserver` |

没做完的事见 [TODO.md](TODO.md)。

测试：`python tests\test_rt.py` 等（每个文件单独跑；用临时目录和假的 AI，不碰真的会话和记录）。

## 代码结构（2026-10-22 起）

原来一个 `roundtable.py` 管一场"当前会议"，现在拆成 `bridge\` 包，每场会议是一个独立的 `Room`：

| 文件 | 管什么 |
|---|---|
| `roundtable.py` | 启动入口（桌面快捷方式、start.bat 跑它），只是调 `bridge.web.main` |
| `bridge\common.py` | 路径、上限、规矩文本、小工具（跑子进程、找 claude / codex 的真身） |
| `bridge\room.py` | 一场会议 = 一个 Room：消息、排队、每个 AI 一把锁、叫 AI、看板、发文件、压缩、停、撤回；会议目录的增删、回收站；`Rooms` 管读进内存的会议 |
| `bridge\agents.py` | 怎么叫 claude（每轮一个 `claude -p`）、codex（每场会议一个 app-server）；模型、MCP / 插件 / 技能开关；护栏钩子 |
| `bridge\guard.py` | 文件护栏：谁占着哪个文件，跨会议 |
| `bridge\mcp.py` | 会议室自己的 MCP 服务（`/mcp`） |
| `bridge\workspace.py` | 干活模式的 git 存档点、改动清单 |
| `bridge\web.py` | HTTP 接口和启动 |
| `bridge\hook_codex.py` | codex 改文件前跑的钩子脚本（问护栏） |
| `index.html`、`static\` | 网页、装成 app 用的 manifest / service worker / 图标 |

## 详细说明

你、Claude Code、Codex 三方在一个网页里开会。分两种模式：讨论模式（默认，AI 只能看不能改）和干活模式（AI 能改项目文件，带存档点，可撤回）。
以会议为单位：一场会议 = 标题 + 目标 + claude 一个固定会话 + codex 一个固定会话 + 聊天记录 + 附件。几场会议可以同时开、同时跑。

- 开：双击 `start.bat`，会自动打开 http://127.0.0.1:8765 。关掉黑窗就是关服务。（后台常驻可以用 `pythonw roundtable.py`，设环境变量 `ROUNDTABLE_NO_BROWSER=1` 就不弹浏览器。）
- 开会：第一次打开没有会议，会弹窗让你填标题、目标和工作文件夹。"新会议"建一场新的、这个标签页切过去，别的会议照常跑。详见下面"几场会议同时开"。
- 归档：列表里每场有"归档 / 取消归档"，只是标记（`meeting.json` 的 `status` = `archived`），内容和 AI 会话都保留；归档的排到列表底部、灰显。老数据的 `status: closed` 读作已归档。
- 每场会议各自的模型和 effort：点顶部 claude / codex 胶囊，里面可改"模型"（下拉预置 + 可手填）和 effort（第一项"默认"）。存在 `meeting.json` 的 `models`，空值 = 用会议室默认。胶囊上显示实际生效值，如 `claude-opus-5-5 · medium`。
  - 会议室默认（2026-10-16 定，`roundtable.py` 的 `ROOM_DEFAULTS`）：claude = `claude-opus-5-5 · medium`，codex = `gpt-6.1-sol · medium`。不再跟随 `~/.claude/settings.json`、`~/.codex/config.toml` 的全局设置，每轮都把生效值明着传；会议室也不改这两个文件。
  - 调用时有值才加参数：claude `--model <m>` / `--effort <e>`；codex 在 `turn/start` 里传 `model` / `effort`（没设就传 config.toml 的全局值）。可选 effort：claude 为 `--effort` 支持的 low / medium / high / xhigh / max；codex 取 config.toml 的 `enabled-reasoning-efforts`，没有就 low / medium / high。
  - 改了会插一条"claude 换成了 X · Y"的系统消息。中途换模型，下一轮会慢一点、费一点（页面会提示）。AI 在跑或排队时不许改（409）。
- 删除会议（两步）：列表每场有"删除"，二次确认后把会议目录移到 `data\trash\<目录名>`（重名加后缀）。这场会议的 AI 在跑时不能删（别的会议在跑没关系）；这个标签页正开着的那场按钮是灰的。列表底部"回收站"可"恢复"（移回 `meetings\`），或者点"清空回收站"一键全删（确认一次）；清空会清掉每场的 `refs/roundtable/<目录名>/*` 的存档点引用，之后那几步干活模式的改动就撤不回了。路径限制在 `meetings\` 和 `trash\` 之内，目录名以服务端列表为准。
  - 清空回收站时，每场会议里 AI 自己的会话也一起删（移进回收站时不删，还能恢复）：这场用过的全部会话 id = 当前的 `claude_session` / `codex_session` + `old_sessions`（摘要续接换下来的旧会话，从这版起才记）。
    - claude：删 `~/.claude/projects/*/<id>.jsonl` 和同名目录、`file-history/<id>`、`session-env/<id>`、`todos/<id>*.json`。
    - codex：经 App Server 发 `thread/delete`（连它的记录文件）；codex 正在跑别的就等那一轮跑完。
    - 别的会议（还在的、回收站里剩下的）也在用的会话 id 不删；id 只认字母数字和横杠，防止拼出别的路径。
    - 在后台删，删完在最近打开的那场会议里插一条系统消息：每个会话删了几项 / 本机没找到 / 没删成的原因。
    - 你在终端里 `claude --resume` 接着聊过、另存成新 id 的会话，会议室不知道，不会删。
- 用：不写 @ 就是 claude、codex 都回；写 `@claude` / `@codex` 只叫被点名的。AI 回复里 @ 对方会触发对方，@用户 不触发，只在页面上高亮。AI 之间来回不限轮数，聊到有一方 @用户 或不再 @ 对方为止。
- 调度三条规则（讨论、干活模式都一样）：
  1. **你发言触发的 AI 调用：并行**。一条消息叫了两个 AI 就同时启动，谁先完谁先发；两个这一轮互相看不到对方的回答（前言里告诉了它们，回完发现冲突再 @ 对方）。
  2. **AI 之间的对话：排队**。由 AI 的回复 @ 另一个 AI 触发的调用，进这场会议自己的串行队列，一个跑完下一个才开始（别的会议各排各的）。排到时如果它该看的消息都已经看过（增量为空），就不白叫。
  3. **同一个 AI 永远串行**（它只有一个会话）：每个 AI 一把锁，同一个 AI 的调用无论来源都排队等它自己；已排上、还没开始的不重复排（它开始时才取增量，自然会带上新消息）。
  - 增量递送：每个 AI 的 last_seen 在它拿到锁、开始被叫时取快照，回复写入后才推进。
  - 状态条 / `state` 的 `running` 是列表，两个 AI 同时在想会都显示。切模式、交接时这场会议有 AI 在跑（或在压缩）就拒绝（409）；压缩本身不拒绝，排在那个 AI 手上的活后面。
- 固定会话：每个 AI 在一场会议里只有一个会话，第一次被叫到时才建（开会本身不花额度）。之后每次只递"自它上次以来别人说的新消息"，它自己的话不重复发。目标改了，下次叫它时会在前面带一句"会议目标改了，新的目标：…"，只带一次。
- 会话名 = 会议标题（2026-10-18 起）：claude 用 `-n <标题>`（新会话起名，老会话 `--resume` 时补上），codex 用 App Server `thread/name/set`。在 claude 的 `/resume` 列表、Codex 桌面应用的会话列表里按会议名就能找到。同一会话名字没变不重复设；服务器重启后每个会话补设一次。老会议的会话下次被叫到时自动起名。
- 目标（原来叫"公告"）：见下面"目标"一节。
- 附件：输入栏回形针选图片/文件（单个 25MB 内），存在这场会议的 `files\`；Codex 会收到这次递给它的新消息里的图片（最多 5 张），Claude 用 Read 读路径。语音功能没做，iPhone 上用键盘自带的麦克风听写即可。
- 交给终端 / 收回：点 claude 或 codex 的状态胶囊 -> "交给终端"。交出去后网页里不再叫它；@ 它的消息照常记录，并提示"现在在终端里，回来后会补看"。窗口里有一条一键复制的命令：
  - `cd "<工作文件夹>"; claude --resume <id>`
  - `cd "<工作文件夹>"; codex resume <id>`
  在终端里干完活，回网页点"收回"，下次叫它时会带上"你刚才在终端里干了一段活"的说明，并补上这期间会议室里的新消息。会话还没建（这场会议没叫过它）时按钮是灰的，先 @ 它一次。它正在跑或排着队时不能交，等说完。
- 压缩：claude、codex 各压各的。点顶部 claude / codex 胶囊，里面有"压缩 claude / 压缩 codex"；或在输入框单独发 `/compact`（两个都压，各自排队、各自回报）、`/compact claude`、`/compact codex`（不当普通消息，不叫 AI）。
  - 每个 AI 的压缩单独开线程，只等它自己的锁（手上那一轮说完再压）；压一个的时候另一个照常能叫。状态里显示 `compact:claude` 这样，页面上是"claude 正在压缩"。已在压的不重复排；压缩不归"停"管。
  - claude：`claude -p --resume <id>` 发 `/compact`，实测会写 compact_boundary 并保留记忆；没压成就退回"写摘要换新会话"（`compact_one` 的退化分支）。
  - codex：经 App Server `thread/resume` → `thread/compact/start` → 等 `turn/completed`（看到 `contextCompaction` 条目算压成）→ `thread/unsubscribe`。2026-10-14 实测（codex-cli 0.162.1）：上下文 30k → 24k，压完记忆还在。没压成就直说，不换会话；它快装不下时也会自己自动压（阈值 `-c model_auto_compact_token_limit=<字数>`）。`ROUNDTABLE_CODEX=exec` 时不支持手动压。
- codex 的模型列表读 `~/.codex/models_cache.json`（codex 自己缓存的清单，只列 visibility=list 的），effort 选项按当前模型取。在终端里的 AI 会被跳过。
- 电脑上 Enter 发送、Shift+Enter 换行；手机上 Enter 换行，点按钮发送。手机上顶部那排按钮收在 ☰ 菜单里。
- 手机（iPhone）：电脑和手机都装 Tailscale，在电脑上运行 `tailscale serve --bg 8765`，再用 `tailscale serve status` 看到的 https 地址在手机 Safari 打开，可"添加到主屏幕"。
- 存储：`data\`（可用 `ROUNDTABLE_LOG_DIR` 改）
  - `current.txt`：最近打开的会议的目录名。页面没带会议号时打开它；外部程序（`/api/post`、MCP）没指定会议时发到这里。
  - `meetings\<YYYY-MM-DD_HHMM>_<标题>\`：`meeting.json`（标题、目标 `announcement`、两个会话 id、每个 AI 的 last_seen / announcement_seen / handed_off、模型、工具开关 `tools`、状态 open/archived）、`log.md`（给人看）、`log.jsonl`（程序读，重启恢复用）、`files\`（附件）。
  - `claims.json`：明着占的文件（文件护栏）；`codex_hook_trust.json`：codex 钩子的信任 hash（见"文件护栏"）。
- 只读保证（讨论模式）：claude 每次带 `--allowedTools Read,Grep,Glob` 加 bridge 的几个只读 / 发言工具；codex 每轮在 `turn/start` 里明着传 `sandboxPolicy: readOnly`（见下面"codex 怎么接"）。讨论模式只接 bridge，你勾的 MCP 只在干活模式加载。
  - codex 没勾的 MCP 服务器逐个关掉（`-c mcp_servers.<名>.enabled=false`，名单每次从 `~/.codex/config.toml` 的 `[mcp_servers.*]` 读），再 `--disable apps / computer_use / browser_use`。自带生图（`image_generation`）2026-10-15 起放开，见"AI 发图片 / 文件"。原因：`-c mcp_servers={}` 实测关不掉，而有的 MCP（比如 node_repl）能读写文件，不关就等于没有只读。

## codex 怎么接（App Server，2026-10-13 起）

- 每场会议一个 `codex app-server --listen stdio://` 子进程（这场会议第一次叫 codex 时起，挂了下次自动重起，闲 20 分钟自动关），在它的输入输出上收发 JSON-RPC。启动参数：`-c windows.sandbox="unelevated" -c notify=[]`，加上面那串关 MCP / 关功能的参数、bridge MCP、护栏钩子、这场会议的插件 / 技能开关。会议换了模式或工具设置，下一轮按新参数重起；"停 codex"只杀这场会议的那个进程。
- 起 claude / codex 时不经过 npm 的 `.cmd` 外壳（那会过一道 cmd.exe，参数里的 `|` `&` 会被当成命令符号，2026-10-22 实测 `--settings` 里的 `Edit|Write` 被拆成了管道）：claude 直接起 `claude.exe`，codex 用 node 跑 `codex.js`（`common.resolve_cmd`）。
- 每轮：`thread/resume`（新会议第一次是 `thread/start`）→ `turn/start`（明着传 cwd、`approvalPolicy: never`、`sandboxPolicy`、模型、effort）→ 等 `turn/completed` → `thread/unsubscribe`。每轮都重新接、说完就放手，所以你在终端里 `codex resume <会话号>` 聊过的，下一轮也接得上；交给终端时会议室不占着会话。
  - 回复取这一轮 `phase: final_answer` 的 `agentMessage`；改了哪些文件取 `fileChange` 条目的路径（started 和 completed 都记）。
  - 接不上原会话（比如会话号不存在）直接报"接不上原会话"，不偷偷新开。服务端发来的审批请求一律拒绝，免得卡住（`approvalPolicy: never` 下本来也不会来）。
  - 模型 / effort 在会话里设一次会一直留着，所以每轮都把生效值（会议自己设的或会议室默认）明着传。
- 为什么是 unelevated 沙箱、为什么去掉 PATH 里的 WindowsApps（2026-10-13 实测，codex-cli 0.162.1）：
  - elevated 沙箱每次跑命令前都要给桌面版运行时里的 `node_repl.exe` 刷权限，桌面版开着就被占住（os error 32），命令一条都跑不了。换一份干净的 `CODEX_HOME` 也一样，跟配置无关。
  - unelevated 起不了商店版 PowerShell 7（`CreateProcessAsUserW` 拒绝），所以 app-server 子进程的 PATH 去掉 WindowsApps，codex 就退回系统自带的 Windows PowerShell。
  - 实测（桌面版开着）：讨论档读得了、写不进；干活档能写项目里、项目外写不进；后台重启后接得上原会话；接得上旧 `codex exec` 建的会话；图片传得进去。
  - 没实测：unelevated 下"关网络"拦得多严。
- 退路：环境变量 `ROUNDTABLE_CODEX=exec` 切回旧的每轮一个 `codex exec`（只能手动切）。旧路在桌面版开着时跑不了命令。

## 几场会议同时开（2026-10-22 起）

- 每场会议各跑各的：各自的消息、排队、每个 AI 一把锁、claude / codex 会话、codex 后台进程都分开。会议甲里 claude 在干长活，会议乙照样能叫、能跑完；"停"只停这场会议的。
- 页面：每个浏览器标签页开一场会议，会议号在地址栏 `?r=<会议号>`。电脑左边栏最上面是会议列表，点一场就切过去（不刷新页面）；Ctrl 点 / 中键在新标签页开，想并排看就开几个标签页。手机在"会议列表"里切。
- 会议列表上的标记：蓝点"xx 在想"= 那场有 AI 在跑；橙色"等你"= 那场有 AI @用户 的话你还没看；"新 N"= 有 N 条新消息没看。"看过"记在这个浏览器里（第一次见到的会议按"都看过"算）。有别的会议在等你时，标题栏带"(N)"，手机的 ☰ 上有个橙点。
- 开新会议、切会议、删别的会议都不用等 AI 说完；只有删"这场"时它不能在跑。
- 外部程序（`/api/post`、MCP）发言时可以带会议号，不带就发到最近打开的那场。

## 目标（原来的"公告"）

- 每场会议一个目标：这场会议要做成什么。页面上原来"公告"的位置，改叫"目标"，"编辑目标"能改。存在 `meeting.json` 的 `announcement`（字段名没改，老会议直接用）。
- 两个 AI 第一次被叫时收到"这场会议的目标：…"，目标改了下次叫它时收到"会议目标改了，新的目标：…"；前言里告诉了它们以目标为准安排事情。bridge 的 `room_info` 工具也能随时看。
- 没接 claude 的 `/goal`、codex 的 goal 功能：它们的意思是"AI 自己一轮接一轮干到它认为达成"（codex 实测：一设目标就自己开跑，没完成就接着跑，多花好几轮），跟会议的目标不是一回事。要不要做成可选的"目标模式（自动推进）"，待定。

## 文件护栏（2026-10-22 起）

- 同一个文件同一时间只让一个 AI 改，跨会议（这场会议的另一个 AI、别的会议里的 AI 都算）。
- 自动占：AI 用编辑工具第一次改某个文件时，会议室替它占住，这一轮说完自动放开；一轮里 30 分钟没再碰也放开。
- 明着占：AI 要跨几轮连着改一批文件时，用 bridge 的 `claim_files` 占（文件或文件夹，写原因和多久，默认 30 分钟，最多 4 小时），做完 `release_files`。明着占的存在 `data\claims.json`，重启还在。
- 你也能占：页面"正在改的文件"一栏的"我在改…"，填完整路径，AI 改它们会被挡。每一条都能"放开"（强行）。
- 怎么挡：
  - claude：`--settings` 里一个 HTTP 的 PreToolUse 钩子（匹配 Edit / Write / MultiEdit / NotebookEdit），改之前 POST 到 `/api/guard/claude` 问一声；不行就回 `deny` 加理由。bypassPermissions 下也照样挡（2026-10-22 实测）。
  - codex：`-c hooks.PreToolUse=[{matcher="apply_patch",...}]` 跑 `bridge\hook_codex.py`，它把补丁全文交给 `/api/guard/codex`，会议室从 `*** Update/Add/Delete File:` 里认出要改的文件。codex 的钩子要先"信任"才跑：第一次起时用一个临时 app-server 查 `hooks/list` 拿到 hash，存进 `data\codex_hook_trust.json`，之后启动带 `-c hooks.state={…trusted_hash…}`。Windows 上 codex 用 PowerShell 跑钩子命令，所以命令写成 `& "python.exe" "hook_codex.py"`（2026-10-22 实测）。
  - 被挡时 AI 看到的理由："文件护栏：<路径> 正被 <谁>（会议「…」）占着…，先做别的，或在回复里跟它 / 用户商量；不要换成命令行去写它。"会议里插一条系统消息"文件护栏挡下了 xx…"。理由里故意不写 "@codex" 这种连着的写法：AI 把原话贴进回复会误叫人。
  - 会议室没开、钥匙不对、出错：放行（护栏是防撞的，不该把 AI 卡死）。
- 管不到的：命令行写的文件（`sed -i`、脚本之类）。规矩里写了不许绕。撤回清单也不记它们。
- 撤回检查也跨会议了：同一个仓库里别的会议的 AI 在同一时间段改过同一个文件，那一步不能撤。

## 工具：这场会议的 MCP / 插件 / 技能

- 点 claude / codex 胶囊，"工具（这场会议）"一栏，勾选后"保存工具设置"（这个 AI 在跑时不能改）。存在 `meeting.json` 的 `tools`。
- MCP：列出你配过的（claude：`~/.claude.json` 的用户级、这个文件夹的本地级、项目里的 `.mcp.json`；codex：`~/.codex/config.toml`）。默认一个不勾（回得快），勾上的只在干活模式加载。会议室自己的 bridge 一直接着。
- 插件：
  - claude：列出这个文件夹里 `claude plugin list --json` 看到的插件。默认跟你平时一样，除了两类默认关：带钩子的（superpowers 这类，开场会往会话里塞一大段说明）、从 claude.ai 同步来的（slack / discord 之类）。开关靠 `--settings` 的 `enabledPlugins`。
  - codex：只有一个"全部插件"开关（实测按个关对远程插件没用，只有 `--disable plugins` 管用）。
- 技能：能一个一个关。claude 靠 `--settings` 的 `skillOverrides`（插件带的技能跟着插件开关走）；codex 靠 `-c skills.config=[{name=…,enabled=false}]`，清单问 app-server 的 `skills/list`。
- claude 的插件清单查一次要一两秒，会议读进来时在后台先查好，缓存一分钟。

## 会议室自己的 MCP（`http://127.0.0.1:8765/mcp`）

- Streamable HTTP，只回 JSON（不开 SSE），协议版本认 2025-11-25 / 2025-06-18 / 2025-03-26 / 2024-11-05。带 `Origin` 头且不是本机的请求一律 403（防网页借浏览器打本机端口）。
- 会议里的 AI：会议室起 claude / codex 时自动接上，带这场会议给它的钥匙。工具：
  - `room_info`：目标、模式、看板、谁在跑、这个文件夹里谁占着哪些文件。
  - `post`：一轮中途往会议里发一句进度或发文件给你看（不叫醒别人，跑很久的活用它报进度）。
  - `board`：改看板。
  - `claim_files` / `release_files` / `list_claims`：文件护栏。
- 外面的 AI（你桌面版、终端里的 Claude Code / Codex）：不带钥匙，工具是 `list_rooms`、`read_room`（目标、看板、最近的消息）、`post_to_room`（以 claude / codex 的身份发言，跟 `/api/post` 一样）。接法：
  - Claude Code：`claude mcp add --transport http ai-bridge http://127.0.0.1:8765/mcp`
  - Codex：`~/.codex/config.toml` 里加 `[mcp_servers.ai-bridge]` 和 `url = "http://127.0.0.1:8765/mcp"`
- 两个 AI 都实测调通过（2026-10-22）。

## 装成 app

- 网页带了 `manifest.webmanifest` 和一个 service worker（`static\sw.js`）：页面、接口、附件一律直连服务器，从不拿缓存顶替；只缓存一张"会议室没开"的提示页，服务器没开时打开 app 看到的是它。
- 电脑：用 Chrome / Edge 打开 http://127.0.0.1:8765 ，地址栏右边"安装"，之后像普通程序一样从开始菜单打开，单独一个窗口。
- 手机：要 https（`tailscale serve --bg 8765` 给的地址），Safari / Chrome 里"添加到主屏幕"。
- 图标由 `make_icon.py` 生成（`static\icon-192.png`、`icon-512.png`、`icon-maskable-512.png`）。
- exe：还没做（要装 PyInstaller）。

## 谁能用哪个接口（身份规则）

发言人只有三种身份：`你`（用户）、`claude`、`codex`，加会议室自己的 `系统`。**每个参与者只能用自己的身份发言，`你` 只能来自用户在网页上的操作。**

- 接口地址：会议相关的都带会议号，`/api/r/<会议号>/<动作>`。GET：`messages?since=`、`state`、`tools?ai=`；POST：`send`、`upload`、`announcement`（目标）、`board`、`mode`、`undo`、`undo/preview`、`models`、`tools`、`handoff`、`compact`、`wrapup`、`stop`、`open`（记成最近打开的）。全局的：GET `/api/rooms`、`/api/trash`、`/api/claims`；POST `/api/rooms/new`、`/api/rooms/archive`、`/api/rooms/delete`、`/api/trash/restore`、`/api/trash/purge`、`/api/claims/release`、`/api/claims/mine`。
- **代表用户的接口，只认浏览器**：上面所有 POST（`/api/post`、`/mcp`、`/api/guard/*` 除外）。要求请求头 `Sec-Fetch-Site: same-origin`（浏览器自动带），或带 `X-RT-Token`（每次启动随机生成，服务端把它注入网页 HTML，前端每个请求自动带）；两者满足其一，否则 403，正文"这个接口只给网页上的用户用；外部程序请用 /api/post 并写明自己的身份"。
  - 诚实说明：这是防误用，不是防故意伪造。curl 想手写 `Sec-Fetch-Site`、或者先 GET 首页拿 token，都做得到。它拦的是"外部程序不小心用用户的身份发了言"。
  - 经 `tailscale serve` 反代后 `Sec-Fetch-Site` 是否还在没有实测确认，所以留了 token 兜底：头被剥掉也能用。
- **`/api/post`，给外部程序用**（桌面应用里的 Claude 主对话、终端里的 Claude / Codex 等；也可以用 MCP，见"会议室自己的 MCP"）：JSON `{"speaker": "claude" | "codex", "text": "...", "room": "会议号（可不填，= 最近打开的那场）"}`，`speaker` 必填，只允许 `claude` 或 `codex`；`你`、`系统`、空、别的名字一律 400。这类消息页面上显示就是普通的 claude / codex，内部带一个不显示的字段 `origin: "external"`（会议室自己叫起来的回复是 `room`）。
  - 递送：外部 claude 的话会递给会议室里的 claude（它不知道外面那个 claude 说了啥），外部 codex 同理；`origin=room` 的才按"不含它自己的话"排除。前言里也告诉了两个 AI "署名 claude / codex 的话有些是会议室外同名 AI 说的"。
  - 路由：按"AI 发言"算。正文里 `@claude` / `@codex` 都会叫会议室里的对应 AI（外部发言的 @ 不按"@自己不算"排除）；`@用户` 标 to_user，不叫 AI。不带 @ 的不叫任何人。
  - 示例：
    `curl -s -X POST http://127.0.0.1:8765/api/post -H "Content-Type: application/json" -d "{\"speaker\":\"claude\",\"text\":\"（测试）不带@的外部发言\"}"`
- 只读接口（GET 的那些、`/files/...`、首页）不限制。旧页面用的 `/api/state`、`/api/messages` 还在（按最近打开的那场回，带 `reload: true` 提示页面刷新）。
- **`/api/guard/claude`、`/api/guard/codex`**：AI 改文件前的钩子来问，带这场会议给它的钥匙（`Authorization: Bearer`），见"文件护栏"。

## 讨论 / 干活模式

- 页面顶部"讨论模式 / 干活模式"按钮（手机在 ☰ 里），切到干活模式会弹确认框；干活模式下标题栏有橙色标记。模式存在每场会议的 `meeting.json`（`mode`），新会议默认讨论。AI 正在回复时不能切。
- 切换时插一条系统消息"切到干活模式 / 切回讨论模式"，递给两个 AI；下次叫它们时前面再带一段"模式变了。…"说明新规矩。
- 干活模式**完全放开**（用户 2026-10-13 定）：跟平时用桌面版 / 终端一样，能改文件、跑命令、联网、用 git、用技能，加载你给这场会议勾的 MCP（默认一个不勾，回得快）。文件护栏照样管。
  - claude：`claude -p --permission-mode bypassPermissions --strict-mcp-config --mcp-config <bridge + 勾的 MCP> --settings <护栏钩子 + 插件 / 技能开关> --output-format stream-json --verbose`（新会话再加 `--session-id`，之后加 `--resume`）。实测能写 `.claude/` 下的文件、能跑 git。最终回复取 `type: result` 事件的 `result` 字段（`-p` 下 stream-json 必须带 `--verbose`）。
  - 钩子：以前用 `disableAllHooks` 把钩子全关（会议室每轮都是新进程，开场钩子每轮都会塞一大段说明），但它会连会议室自己的护栏钩子一起关，2026-10-22 起不用了。用户的钩子目前只来自插件，带钩子的插件（superpowers）在会议室里默认关，见"工具"。
  - codex：`thread/resume` 传 `sandbox: danger-full-access`，`turn/start` 传 `sandboxPolicy: dangerFullAccess`。
  - 撤回只管得到编辑工具的改动（claude 的 Edit/Write、codex 的 apply_patch）；命令写的文件、删掉的文件、git 操作撤不回，步骤消息里会标"它还跑了 N 条命令，命令写的文件不在记录里"。
- 前言按模式给 AI 写规矩：干活模式下动手前先 @ 对方说清要改哪些文件，别同时改同一个文件（文件护栏会挡，被挡了不许换命令行绕过去）；删文件、git commit / push / reset、装软件、动项目外的东西这类撤不回的事，先在会议里说清楚、用户没反对再做；能用编辑工具改的别用命令写；改完 @用户 汇报。

## 落盘

- 顶部"落盘"按钮（手机在 ☰ 里），点了弹确认框。claude 在终端里、正在回复或排队时按钮是灰的。
- 点了以后插一条系统消息（只递给 claude，codex 看不到），叫 claude 把这场会议定下的东西落盘：工作文件夹里有 `.claude/skills/wrapup/SKILL.md` 就照它做（里面要用户在终端敲的步骤，比如提醒 /compact，在会议室里省掉），没有就用通用版（见下面"工作文件夹"一节）；然后只提交这次落盘改到的文件并推送，做完 @用户 汇报改了哪些文件、提交号、推送成没成。
- 这一轮 claude 强制按干活模式（完整权限）跑，不管会议现在是什么模式；会议本身的模式不变，下一轮回到原模式。实现：`state["force_work"]`，`process_job` 一开头就取走这个标记（提前返回也不会留到下一轮），再把模式明着传给 `call_ai`。
- 没有能落盘的结论时它会直说，不硬写。

## 强行打断（停）

- 有 AI 在跑或排队时，状态行（"xx 正在想…"）右边按人出红色按钮："停 claude""停 codex"，两个都在忙时再多一个"全停"。点了弹确认框。`/api/stop`，只给网页用；body `{"ai": "claude" | "codex"}`，不填 = 全停。
- 效果：被停的 AI 连同它起的命令（比如 git push）一起杀掉（claude 杀 `claude -p` 进程树；codex 杀这场会议的 app-server 进程树，下一轮自动重起、会话从记录里接上；别的会议不受影响）；它排着的全部作废；另一个 AI 照常跑，压缩不受影响。会议里插一条系统消息，两个 AI 下一轮都能看到。
- 实现：`state["epoch"]` 是每个 AI 一个计数，停了谁就给谁加一；排队时记下 `job_tag(ai)`（会议 id + 这个 AI 的计数），开跑时对不上就放弃；被打断的 AI 记在 `state["stopped"]`，收尾时报"被你打断了"。刚好在停的那一刻说完的话照登，但里面的 @ 不再叫人。
- 页面靠 `/api/state` 里的 `stop_each` 判断后端认不认按人停；旧后端只给一个"停"（= 全停）。
- 已经改了的文件不会自动还原（干活模式下可以按步骤撤回）；命令做到一半的事（比如推送）可能只完成一部分。

## 粘贴图片、拖文件

- 输入框里直接 Ctrl+V：剪贴板里有图片（截图、复制的图片）或文件就当附件上传，名字是"粘贴_时分秒.png"；剪贴板同时有文字时，文字照常粘进输入框。
- 把文件拖到页面上任意位置也会上传（拖进来时底部输入区有虚线框）。

## AI 发图片 / 文件

- AI 在回复里单独起一行 `【发送】文件路径`（也认 `[发送]`；完整路径，或相对工作文件夹的路径；路径两边的引号、反引号会去掉）。会议室收走这一行，把文件复制进这场会议的 `files\`，挂在它这条消息上：图片在页面上直接显示，别的文件可下载。规矩写在前言里，已有的会话下一轮会收到新版规矩。
- 正文里指向本机路径的 markdown 图片 `![说明](D:/…/x.png)`（也认 `<路径>`、`file:///` 网址、相对工作文件夹的路径）也当发送：图片挪成附件，这段 markdown 从正文里去掉；没发成就留着原文。网址（http、data: 等）不动。起因：codex 习惯这么写，网页又显示不了本地路径（2026-10-14 它发九张概念图，用户只看到一串路径）。
- 规矩里写了：已经知道是哪个文件、只是发给用户时直接写 `【发送】`，不用先打开看（打开只是它自己看到，还白占上下文；那次 codex 为了"重发"把九张大图全打开，下一次请求 4 分钟没回音）；要靠看内容才能挑出来时照常看，先用文件名和文件夹缩小范围，一次少看几张。
- 一条最多 10 个，单个 25MB 内（跟用户上传一样）。代码块（包括行内代码）里的 `【发送】`、markdown 图片不算。本来就在会议附件里的文件不重复复制。
- 没发成（找不到、太大、超个数）插一条系统消息"xx 的文件没发成：…"，不是 quiet 的，发的人下一轮能看到。
- 另一个 AI 照常收到：消息里带 `[附件 图片] 路径`，codex 还会拿到图片本身。外部程序经 `/api/post` 用 claude / codex 身份发言时也认这一行。
- 路径不限目录：AI 本来就读得到的文件都能发，只发给网页上的你看。
- 2026-10-14 用真 claude 试过：叫它把工作文件夹里的图发过来，它照规矩写了 `【发送】`，图挂上了。codex 没实测。

### codex 自带生图

- codex 的自带生图（OpenAI 图像模型，不占本机显卡，用用户 ChatGPT 账号的额度）两种模式都开着。App Server 推来 `imageGeneration` 条目时：开始了就在状态行显示"正在生成图片…"；完成了取 `savedPath`（在 `~/.codex/generated_images/<会话号>/` 下），会议室自己在它的回复后面补一行 `【发送】路径`，图就挂上了，不靠它记得发。它自己也写了同一张的只发一次（`【发送】` 按路径去重）。
- 生图失败（比如额度用完，`failure.type = usageLimitExceeded`）在它的回复后面补一句"（生图额度用完了（几号几点恢复））"。
- 规矩里写了：出概念图、氛围图优先让 codex 用自带生图；本机显卡（ComfyUI、渲染、训练）同一时间只让一个人用，动手前先 @ 对方说一声；说"出了一张"时用 `【发送】` 发出来。
- 2026-10-15 实测：让 codex 画"白底红点"，63 秒，图挂上了（它那次自己画了两张，两张都挂）。只在 App Server 方式下有，`ROUNDTABLE_CODEX=exec` 不处理。

## 跳到我的发言

- 聊天区右下角两个圆按钮（内容超过一屏才出现）：点 ▲ 跳到上一条你说的话，点 ▼ 跳到下一条（后面没有就到底）；跳到的那条闪一下。
- 长按（半秒）▲ 直接到顶，长按 ▼ 直接到底。手机上长按不弹菜单。
- "上一条"指顶在屏幕上沿之上的；已经在屏幕里的不算。往上没有了时 ▲ 变淡（再点就到顶），已经在底部时 ▼ 变淡。
- 最上面的 ☰ 是**目录**：列出你在这场会议里说过的每一句（时间 + 前两行，带附件的标 📎 几个），当前看到的那条高亮；上面能搜。点一条就跳过去。电脑上目录开着不关，方便连着跳；手机上是底部抽屉，点完自动收起。

## 看图 / 标注

- 点消息里的图，就在当前页打开大图（不再开新标签页）；想开新标签页就 Ctrl 点，或者在大图里点"原图"。
- 左右切换：← → 键、两边的箭头按钮，手机上左右滑。切换范围是这场会议里所有的图，按出现顺序。点图看原尺寸，Esc 或点空白处关闭。
- **标注 / 裁剪**：画笔、箭头、框、文字、裁剪（拖出要留的区域，松手就裁），6 种颜色、细 / 中 / 粗三档，可以撤销（Ctrl+Z）。
- 改完点"放进输入框"：存成 `原名_标注.png` 放进待发附件，配句话发给 AI 就行；"存到本机"是直接下载。
- 输入框里还没发出去的图也能点开改，改完替换掉原来那张。

## 引用回复

- 电脑（鼠标）：在消息上右键。选中了这条里的一段文字就出"引用这段回复 / 复制 / 引用整条回复"，没选就只有"引用整条回复"。系统消息上右键是浏览器自己的菜单。
- 手机（触屏）：每条消息头上有"↩ 引用"。先长按选一段字再点它，引用的就是这段；没选就引用整条。
- 引用后输入框上方出一条"回复 xx：…"，× 取消。发出去的消息顶上带灰色引用块（只引一段时带引号），点它跳回原文并闪一下。
- 发送时带 `reply_to`（消息编号），只引一段时再带 `reply_part`（选中的文字）；后端只认会议里真有的、你或 AI 说的话（系统消息不能引用），存进这条消息的 `reply_to`：整条就摘原文前 200 字（去掉 markdown 符号），一段就存那段（也截到 200 字），带 `part: true`。
- 递给 AI 的格式：整条是 `[你] （回复 [claude] 的“…”）正文`，一段是 `[你] （回复 [claude] 那条里的这段：“…”）正文`。
- 路由：没 @ 人、引用的是某个 AI 的话，就只叫那个 AI（等于回它）；@ 了人就按 @ 走；引用你自己的话又没 @，照旧两个都叫。

## 工作文件夹（不只这个项目能用）

- 开新会议时填"工作文件夹"（默认带上这个标签页开着的那场的，下拉里有用过的；一场会议都没有时用 `ROUNDTABLE_DEFAULT_DIR`，没设就是用户目录）。两个 AI 都在那个文件夹里跑：claude 进程的工作目录、codex 会话的 cwd、干活模式的 git 存档 / 撤回、"交给终端"的 cd，都跟着它走。存在 `meeting.json` 的 `workdir`。
- 开会后不能改：claude 的会话按文件夹存，中途换了 `--resume` 接不上。换文件夹就开新会议。
- 会议记录、附件存在会议室自己的 `data\`。工作文件夹里不含 `data\` 时，claude 会多带 `--add-dir` 指向它，好读附件。
- 前言只写通用说法 + 工作目录路径；各项目自己的规矩由 AI 自己从那个文件夹的 CLAUDE.md / AGENTS.md 读。
- 不是 git 仓库：讨论模式照常；干活模式照常改，但每步改了什么记不下来、撤不回（每场会议提醒一次）。
- git 仓库：存档 / 撤回照常，私有 index 每个仓库一份（`data\snapshot-<哈希>.index`）。
- 落盘：工作文件夹里有 `.claude/skills/wrapup/SKILL.md` 就照那份技能走；没有就用通用版（照那个文件夹的规矩写进合适的文档，没有合适的就新建 .md 纪要；是 git 仓库就提交，有远端就推）。
- 已知限制：讨论模式下 codex 在只读沙箱里，读不了只给你本人账户开权限的文件夹（实测 `AppData\Local\Temp` 下的读不了，`D:\` 下普通文件夹没问题；桌面、文档这类用户目录可能也一样）。干活模式不受影响。
- 页面：电脑边栏标题下面显示 📁 完整路径；窄屏 / 手机在标题后面带文件夹名（只在不是默认文件夹时）。

## 看板（公告 + 待办）

- 每场会议一块看板，存在 `meeting.json` 的 `board` 里（`board_seq` 是编号计数）。条目两种：公告（📌）、待办（可勾），每条记谁写的。最多 100 条，每条 500 字以内。
- 你：在页面上直接加、勾、删（`/api/board`，只给网页用；`{op: add|done|undone|del, kind: todo|note, text, id}`）。
- 两个 AI：在回复里单独起一行写 `【公告】…`、`【待办】…`、`【完成】编号`、`【删】编号`（方括号 `[待办]` 也认）。这几行会被收走、不显示在聊天里；代码块里的不算。外部经 `/api/post` 发的也认。
- 每次改动插一条系统消息"看板：xx 加了待办 #3：…"，另一个 AI 下一轮会看到（改的那个自己不重复收）。AI 第一次被叫、压缩后续接时会带上整块看板。这套写法写在会议室规矩里，老会话下一轮会补收到。
- 电脑：左边栏目标下面常显。窄屏 / 手机：目标栏右边"看板 N 待办"按钮，点开展开。
- AI 也能用 bridge 的 `board` 工具改看板（效果一样）。

## 上下文显示

- 两个 AI 的状态卡片多一段"上下文 41%"，到 80% 变红；鼠标停上去看具体数（如 412k / 1M）。
- 数据是上一轮回话结束时的：claude 取 stream-json 里最后一次调用的 `usage`（输入 + 缓存读 + 缓存写 + 输出，子助手的不算），窗口大小取 `result.modelUsage` 里主模型的 `contextWindow`；codex 取 App Server 的 `thread/tokenUsage/updated`（`last.totalTokens` / `modelContextWindow`）。
- 存在 `meeting.json` 的 `ctx` 里，重启不丢。还没回过话、或刚真压缩完时不显示，下一轮回话后再出现。

## 排队和时限

- 你发话不会被 AI 之间的串行队列挡住：某个 AI 已经排在队列里时，你一说话它就马上另开一个并行的；队列里那份轮到时发现该看的都看过了，会自己跳过。
- 时限按"多久没动静"算（2026-10-15 起）：AI 一轮里多久没有任何输出就停（讨论模式 10 分钟、干活模式 20 分钟），另有一轮总上限（讨论 30 分钟、干活 2 小时）。claude 看 stream-json 的每一行，codex 看 App Server 推过来的每一条消息。停的时候系统消息说清是"N 分钟没动静"还是"超过总上限"。
  - 起因：claude 在本机 ComfyUI 出图，开后台任务后一直等结果，中间两段十几分钟一声不吭，被原来"一轮总共 30 分钟"掐掉了。规矩里也告诉了它们：跑久的活先说在干什么、大概多久，等后台任务时隔一阵说一句进度，真要等很久就先结束这一轮。
- 中途的话实时显示：AI 一轮里说的中间话（claude 的文字块、codex 的 commentary、codex 开始生图时的"正在生成图片…"）放进 `/api/state` 的 `live`，页面状态行"xx 正在想…（12 分钟）"下面显示最近一句，点一下展开。不进会议记录、不递给另一个 AI；这一轮结束就清掉。
- 规矩改了（比如干活模式放开）会补给已有会话：`mode_seen` 存的是"模式 + 规矩文本指纹"，对不上就在下一轮前面补一段"会议室规矩改了，以这一版为准"，只补一次。

## 页面布局

- 电脑（宽 ≥1000px）：左边栏最上面是会议列表，下面放这场会议的标题、模式标记、按钮、两个 AI 的状态卡片（名字 / 模型 / 状态各一行）、完整目标、看板、正在改的文件；右边是聊天，内容居中、最宽约 860px。
- 中等宽度（601–999px）：单栏，内容居中。
- 手机（≤600px）：按钮收进 ☰，打开是两列大按钮；AI 状态只显示名字和状态，模型名收起；@ 快捷按钮可横滑；发送、附件、撤回按钮加大。
- claude / codex 的消息按 markdown 显示（标题、粗斜体、列表、引用、表格、代码、http(s) 链接），先转义再套标签；你和系统的消息原样显示。
- 改动归属（按 AI 自己的操作记录，不再是"前后两张快照的差"）：只有这个 AI 自己写文件的操作里出现过的文件，才算它这一步的改动。别的会话、别的进程同期改的文件（比如另一个窗口同时在出图）一概不算，撤回也不碰。
  - claude：从 stream-json 里收 `tool_use` 为 Edit / Write / MultiEdit / NotebookEdit 的 `file_path`（NotebookEdit 是 `notebook_path`）。
  - codex：从 `--json` 里收 `item.started / item.completed` 里 `type: file_change` 条目的 `changes[]`（每项 `path` + `kind`：add / update / delete）。
  - 路径统一规范成相对仓库根的正斜杠路径；项目外的路径单独标"⚠ 改了项目外的文件"（撤不回）。超时、崩溃时已经读到的记录也会用上。
  - "改前"：干活模式叫 AI 前拍一张 git 快照（临时私有 index `data\snapshot-<哈希>.index`，不碰你的 index 和当前分支，`git status` 里看不到；首次约 4 秒，之后零点几秒）。并行时两个 AI 各拍各的。生成提交挂在 `refs/roundtable/<会议目录名>/<序号>`，防 gc。`.gitignore` 忽略的文件和会议室自己天天写的 `meetings\`、`current.txt`、`snapshot.index` 不在快照里。
  - "改后"：AI 跑完后，对记录里的每个文件读当前字节算 sha256 存进这一步，不再拍第二张整体快照。碰了但内容没变的文件不算。
- 改动清单：有改动就插一条系统消息"claude 改了这些文件：改/增/删 路径…"，递给另一个 AI（不递给改的人自己），页面上这条带"撤回这一步"按钮。并行时两个 AI 时间上重叠又改了同一个文件，后记录的那条会标"⚠ 和 xx 同时改了这个文件"。
- 撤回：点按钮先弹确认框，列出要动的文件（新增→删除、修改→还原成改前、撤不回的不动），确认后才执行（页面先调 `/api/undo/preview` 取清单，确认后调 `/api/undo`）。只动这个 AI 这一步记录里的文件：改前存在的从它自己的改前快照按字节写回（`git cat-file blob`），改前不存在的删掉（空目录一并清）。写回前先检查：记录里每个文件现在的 sha256 必须等于这一步记下的"改后"，不等（被另一个 AI、你或别的进程又改过）整步拒绝，一个文件都不动，并列出对不上的文件。撤回后插系统消息"已撤回 xx 的这一步"。有 AI 在跑时不能撤。
- 注意：**被 git 忽略的文件改了撤不回**（改前快照里没有它）：它出现在记录里时清单标"撤不回"，其余文件照常撤；会议室自己的 `meetings\` 等目录同理。旧版（整体快照差异）记下的步骤仍能撤，按旧办法核对。
- 存档点 ref 不会自动清理。手动清理：
  - 看有哪些：`git for-each-ref refs/roundtable`
  - 删一场会议的：`git for-each-ref --format="%(refname)" refs/roundtable/<会议目录名>/ | ForEach-Object { git update-ref -d $_ }`
  - 全删：`git for-each-ref --format="%(refname)" refs/roundtable | ForEach-Object { git update-ref -d $_ }`，再 `git gc --prune=now` 才真正回收空间。删了 ref 之后那几步就撤不回了。

## 许可证

MIT，见 [LICENSE](LICENSE)。
