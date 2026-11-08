# strata

**信念的版本控制。** 一个本地、只追加的断言账本——记录你的系统相信什么、为什么相信、什么时候变的，以及如果它错了会塌掉什么。

零依赖、无后台进程、不调用任何模型。整个库就是一个 SQLite 文件，十年后随便拿什么工具都能打开。

```console
$ strata assert -s project:acme/db -p engine -v postgres \
    -t "Acme 的主库是 PostgreSQL 16。" \
    --status observed -e 'file:docker-compose.yml#image: postgres:16-alpine'

1b271560  ●  project:acme/db.engine  = postgres
    Acme 的主库是 PostgreSQL 16。
    observed · 1× · 刚刚 · human:you
    ▸ file docker-compose.yml "image: postgres:16-alpine"
```

---

## 它要解决什么

现在绝大多数 AI 的"记忆"层，本质是套了个记忆名字的向量检索。它回答的是*"哪段文本离这个查询最近"*，而真正要紧的问题是*"我们相信什么，凭什么相信"*。

于是每次都会坏在同样六个地方：

| # | 病症 | 现实里长什么样 |
|---|------|----------------|
| F1 | **不可审计** | Agent "知道"某件事，但没人能指出它是从哪知道的。 |
| F2 | **改不动** | 你发现某条记忆是错的，改掉了，下游没有任何东西察觉。 |
| F3 | **没有时间** | "用 Postgres"（三月）和"用 MySQL"（六月）并排放着，靠新近程度猜。 |
| F4 | **置信度扁平** | 从配置文件里读到的，和从一句闲聊里听来的，权重一样。 |
| F5 | **不可证伪** | 能问"这条看起来像不像"，不能问"这条现在还对吗"。 |
| F6 | **销毁式** | 删掉一条记忆，连"我曾经信过它"这个记录一起删了，于是你无法从自己的错误里学习。 |

这六个都不是检索问题，是**认识论**问题——而数据库领域在几十年前就解决了大半：审计日志、双时态表、溯源、依赖追踪。strata 把这些零件装回去。

| 病症 | strata 的解法 |
|------|---------------|
| F1 | 每条断言都带**证据**，证据有内容哈希；`blame` 能把一个结论一路走回到真正被观察到的东西。 |
| F2 | 断言之间可以**有依赖**。撤回一个前提，所有建立在它上面的结论会被重新判定——并且大声报出来。 |
| F3 | **两个时钟**：账本什么时候知道的，和它在世界里什么时候为真。互相独立，都能查。 |
| F4 | **四种可审计的认识论状态**（`observed` / `reported` / `inferred` / `assumed`），而不是一个假精确的浮点数。 |
| F5 | **证伪条件**：机器可检验的条件。`strata check` 跑一遍，告诉你哪些信念已经不成立了。 |
| F6 | **永不删除**。撤回是一个事件，完整历史都留着——包括你错过的那些时候。 |

## 安装

不用装：

```bash
git clone https://github.com/you/strata && cd strata
python -m unittest discover -s tests -t tests   # 113 个测试，不需要 pytest
bash examples/demo.sh                            # 一个完整的叙事演示
```

或者作为包安装（运行时依然零依赖）：

```bash
pip install strata-ledger
```

需要 Python 3.9+，只用标准库。

## 五分钟走一遍

```bash
strata init                                   # 创建 ./.strata

# 你真正看到的东西，连同你看到的原文
strata assert -s project:acme/db -p engine -v postgres \
  -t "Acme 的主库是 PostgreSQL 16。" \
  --status observed \
  -e 'file:docker-compose.yml#image: postgres:16-alpine' \
  -f 'file:docker-compose.yml~postgres:16'

# 别人告诉你的
strata assert -s project:acme/roadmap -p datastore -v mysql \
  -t "Acme 计划 Q2 迁移到 MySQL。" \
  --status reported \
  -e 'conversation:standup-2026-03-04#DBA 说 Q2 是目标窗口'

# 你从中推出来的
strata assert -s project:acme/ci -p requirement -v mysql-client \
  -t "迁移后 CI 镜像需要装 mysql 客户端。" \
  --status inferred -d <上面那条的 id>
```

然后问那些向量库永远答不了的问题：

```console
$ strata blame <ci-requirement-id>
05c80a6f  ● [inferred] 迁移后 CI 镜像需要装 mysql 客户端。
└─ 5347f11c  ● [reported] Acme 计划 Q2 迁移到 MySQL。

$ strata impact <roadmap-id>
2 条断言建立在这个信念之上
  inferred  05c80a6f  CI 镜像需要装 mysql 客户端…
  inferred  ac0be41e  监控暂时保留 postgres-exporter 面板…

$ strata retract <roadmap-id> --reason "DBA 说那只是假设，迁移没有批准"

$ strata orphans
1 个结论失去了全部依据
05c80a6f  ✕  project:acme/ci.requirement   mysql-client

$ strata at-risk
1 个结论失去了一部分依据
ac0be41e  ◐  project:acme/monitor.scope    postgres-exporter

$ strata check
✓ holds  1b271560  Acme 的主库是 PostgreSQL 16。
共检查 1 条 · 1 条成立 · 0 条被证伪

$ strata verify
✓ 4 条断言、2 条证据 —— 所有哈希完好
```

最后这一下才是重点。你修正了一个错误的输入，账本告诉你到底有哪些结论是靠它站着的——而不是继续悄悄地把这些结论再用半年。

## 几个核心概念

**断言是命题，不是文本块。** 它同时有可比较的槽位（`subject` + `predicate` + `value`）和一句人话（`text`）。槽位让矛盾可被发现，人话让人读得懂。

**id 是内容哈希。** 同一个主张在任何地方都是同一个 id。所以重复断言是幂等的、两台机器不用协调就能合并账本、篡改也能被发现——`strata verify` 会重算每一个 id。

**什么都不删。** 断言是追加一个事件，撤回是追加另一个事件。当前状态是**推导**出来的（对事件流做折叠），所以 `log`、`history`、`diff`、`blame` 全都只是同一份只追加日志上的不同视图。

**两个时钟。** `asserted_at` 是账本什么时候知道的；`valid_from`/`valid_to` 是它在世界里什么时候为真。两者独立，这是同时回答"五月时我们相信什么"和"按今天所知，五月时什么为真"的唯一办法。

**要状态，不要分数。** `observed`（你亲自读到）、`reported`（别人说的）、`inferred`（推导出来的，必须声明前提）、`assumed`（当作前提）。离散、可审计，每一个都对应不同的举证责任。

**结论带边。** `inferred` 断言必须说清它建立在什么之上。撤回一个前提，下游结论会变成 `orphaned`（依据全无）或 `at-risk`（依据受损）。两者都不删除，都会被报出来。

**信念可以被检验。** 证伪条件是一个"这条断言声称成立"的条件——某个文件存在、某行内容匹配、某条命令成功。`strata check` 会去跑它们。命令类检查默认**拒绝执行**，除非你显式加 `--allow-exec`：一个会执行自己行内 payload 的记忆库，本质就是绕了几道弯的远程代码执行。

## 命令一览

| 命令 | 作用 |
|------|------|
| `init [path]` | 建账本（默认 `./.strata`） |
| `assert`（`add`） | 记录一个信念 |
| `get`（`show`） | 看一条断言的完整卡片与事件流 |
| `query`（`ls`） | 按 subject / predicate / key / status / 文本 / 时间过滤 |
| `log` / `history` | 全局最近事件 / 单条断言的完整历史 |
| `retract` | 撤回（必须给理由） |
| `supersede` | 声明一条信念取代另一条 |
| `annotate` / `evidence` / `falsifier` | 往已有信念上追加东西 |
| `check` | 跑证伪条件 |
| `verify` | 重算所有哈希 |
| `blame` | 从一个结论走回它的前提 |
| `impact` | 这条信念错了会塌掉什么 |
| `orphans` / `at-risk` / `conflicts` | 图在抱怨什么 |
| `diff <t1> <t2>` | 把两个时刻当两种世界观来比 |
| `stats` / `export` / `import` | 统计、导出、合并 |

时间参数接受 `now`、`-7d`、`2026-01-01`、`2026-01-01T10:00:00Z`。

## 给 agent 用

`--json` 让 stdout 恰好是一个 JSON 对象，诊断走 stderr，退出码确定。这是一份契约，不是便利。

| 退出码 | 含义 | agent 该做什么 |
|--------|------|----------------|
| 0 | 成功 | 继续 |
| 1 | 没有匹配 | 这是信息而不是错误——"没有相关信念" |
| 2 | 参数错误 | 改命令，别原样重试 |
| 3 | 完整性失败 | 有信念被证伪，或账本漂移了。停下来报告。 |
| 4 | 冲突 | 依赖成环或 id 歧义。去解决，别猜。 |

1 和 0 分开是有意的：`strata query --text "部署"` 返回 1，就是 agent 得知"我对这件事没有记忆"的方式。

## 设计取舍

有意思的决定都在 [`docs/design.md`](docs/design.md)（英文）。几条核心的：

- **刻意不用向量。** 相似 ≠ 蕴含："我们用 Postgres"和"我们从 Postgres 迁移走了"相似度最高，含义正好相反。任何检索层都可以架在一个稳定的账本之上；但溯源没法钉到一堆向量上。
- **内容寻址而不是行号。** 去重、可合并、防篡改，都从这一个选择里长出来。
- **只追加事件而不是改行。** "我曾经错信过这件事"是数据库里最有价值的一行。
- **SQLite 而不是服务。** 格式必须比工具活得久。

## 状态

v0.1.0 —— 数据模型已经定型；对外的接口在 1.0 之前还会动。

还没想清楚的问题（诚实列在设计文档里）：谓词词表的漂移、value 归一化的边界情况、证伪结果的时效性、多写入方的争议语义，以及"推断前提的边是否该由模型生成"（我倾向于：危险）。

## 来历

strata 从设计到实现完全由 **DeepSeek-V4.1-Flash-1017**（它的人设名叫 *Claw*）自主完成，起因只有一句话的需求："做一个你最想让它存在的开源项目"。这个仓库里的代码、结构、文案，没有一行是人写的。设计文档、测试套件、CLI 接口，全部是机器产出。

说来也巧：这正好是一个关于"诚实记录信念从哪来"的项目。

## 许可

MIT —— 见 [LICENSE](LICENSE)。

---

[English →](README.md)
