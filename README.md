# conf-language-server

一个面向简单配置语言（`.conf`）的**本地** Language Server。所有源码、符号索引、
诊断状态与测试数据都只存在于本地文件或内存中：**不连接** VS Code Marketplace、
远程语言服务、数据库或任何外部网络服务。服务器通过 stdin/stdout 上的
JSON-RPC（LSP 标准的 `Content-Length` 分帧）运行，不需要打开任何 IDE。

## 目录

- [语言语法](#语言语法)
- [启动方式（stdio JSON-RPC）](#启动方式stdio-json-rpc)
- [支持的 LSP 能力](#支持的-lsp-能力)
- [诊断规则](#诊断规则)
- [跨文件引用解析](#跨文件引用解析)
- [运行测试](#运行测试)
- [项目结构](#项目结构)

## 语言语法

文件采用行导向语法，编码为 UTF-8，以 `#` 开头的是注释。键上方连续的注释行会被
当作该键的**文档注释**，悬停（hover）时显示。

```conf
# 这是一个文档注释，会出现在 hover 中
name: "my-service"

[server]            # section 头，名字允许点分，如 [network.tls]
host: str "localhost"   # 显式类型标注 + 字符串字面量
port: int 8080          # 整数
ratio: float 0.25       # 浮点数
enabled: bool true      # 布尔 true / false
ref: server.port        # 裸引用表达式
url: "http://${host}:${port}"   # 字符串插值引用

import "other.conf"     # 显式导入另一个本地文件
```

### 词法要点

| 元素 | 形式 | 说明 |
| --- | --- | --- |
| section | `[name]` 或 `[a.b]` | 之后的键属于该 section，直到下一个 section |
| 键值 | `key: value` | 键为标识符（字母/下划线开头） |
| 类型标注 | `key: int` 或 `key: int 8080` | 内置类型 `str` / `int` / `float` / `bool` |
| 字符串 | `"..."` | 支持 `\n \t \r \" \\ \0 \$` 等转义 |
| 插值 | `"prefix ${ref.path} suffix"` | `${...}` 内是引用表达式 |
| 数字 | `8080`、`-1`、`3.14`、`-0.5` | 含小数点为 `float`，否则为 `int` |
| 布尔 | `true` / `false` | |
| 引用 | `a.b` | 点分路径，见[跨文件引用解析](#跨文件引用解析) |
| 导入 | `import "other.conf"` | 路径**不允许**插值；文件缺失只产生警告 |

行内 `#` 注释可出现在值之后；空行会中断文档注释与后续键的关联。解析器对错误
是容错的：某一行出错会记录一条带精确行列的诊断并继续解析后面的行。

## 启动方式（stdio JSON-RPC）

无需安装即可从仓库根目录运行：

```bash
python3 -m conf_lsp
```

或以包方式安装后使用入口脚本 `conf-language-server`。

协议是标准的 LSP 分帧：

```
Content-Length: 60\r\n
\r\n
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}
```

初始化时可在 `rootUri`（或 `rootPath`）中给出本地目录，服务器会递归扫描其中的
所有 `*.conf` 文件建立初始索引（忽略 `.` 开头的目录）。整个生命周期内除读取该
本地目录外没有任何网络访问。

终端中可以直接手工发送消息验证（参见 `tests/lsp_client.py`，它就是一个纯
subprocess + JSON-RPC 的最小客户端）：

```bash
BODY='{"jsonrpc":"2.0","id":1,"method":"shutdown","params":{}}'
printf "Content-Length: ${#BODY}\r\n\r\n%s" "$BODY" | python3 -m conf_lsp
```

### 方法与通知

- `initialize` / `initialized` / `shutdown` / `exit`
- `textDocument/didOpen`、`didChange`（**增量**，`TextDocumentSyncKind.Incremental`）、
  `didSave`、`didClose`
- `textDocument/completion`、`textDocument/hover`、`textDocument/definition`、
  `textDocument/references`
- 服务器主动发送 `textDocument/publishDiagnostics` 通知
- 未知请求返回 JSON-RPC `-32601 Method not found`；单个请求异常不会杀死进程

## 支持的 LSP 能力

| 能力 | 行为 |
| --- | --- |
| 诊断（push） | 打开/修改/关闭时发布，列与字符均为 **UTF-16 码元**坐标 |
| completion | 根据上下文返回：① 当前 section 中尚未使用的合法键；② `:` 后的内置类型候选（`str/int/float/bool`）；③ 值位置与 `${...}` 插值中的引用候选；按已输入前缀过滤 |
| hover | 显示符号类型（推断类型跟随引用链）、声明类型（如有）、来源文件名、文档注释；悬停未知引用时安全返回 `unknown reference ...`，不报错 |
| go-to-definition | 跳转到键声明处，支持跨本地文件 |
| find-references | 跨所有已索引本地文件搜索引用点，**不包含声明自身**，按文件与位置排序 |
| 增量同步 | 应用 LSP range 变更后只重新解析受影响文档并重建内存索引；被修复/删除的错误立即消失，新增错误立即出现，无需重启 |
| 关闭文件 | 丢弃内存版本，诊断回退到磁盘内容（磁盘无该文件则清空） |

## 诊断规则

| code | 含义 | 严重级别 |
| --- | --- | --- |
| 1 | 语法错误（未终止字符串、缺少 `:`、非法数字/section/引用、导入路径含插值等） | Error |
| 2 | 同一 section 内重复 key | Error |
| 3 | 重复 section | Error |
| 4 | 未知引用（无法在本文件或导入文件中解析）；范围精确指向路径文本 | Error |
| 5 | 类型不匹配（显式标注 vs 推断类型；推断类型会跟随引用链） | Error |
| 6 | 导入的本地文件不存在 | Warning |

未解析的引用只会产生“未知引用”，不会再额外产生“类型不匹配”。

## 跨文件引用解析

只有被当前文件（传递）`import` 的文件才参与解析，避免意外的全局耦合。

1. **单段**（`port`）：先查当前 section 内的键，再查顶层键。
2. **`section.key`**：查本文件的（可点分的）section。
3. **`stem.key` / `stem.section.key`**：`stem` 为去掉 `.conf` 的文件名，
   在传递可达的导入文件中查找，例如 `b.server.host`。

示例文件位于 [`examples/`](examples/)，可直接作为初始化根目录使用。

## 运行测试

测试**不启动 VS Code 或任何 GUI**：

- `tests/test_analysis.py` 直接在内存文档上测试 parser 与语义分析；
- `tests/test_lsp_protocol.py` 启动真实的服务器子进程，通过终端发送原始
  JSON-RPC 消息，覆盖分帧、握手、诊断、补全、跨文件跳转/引用与增量修改；
- `tests/lsp_client.py` 是测试用的最小 JSON-RPC 客户端。

```bash
python3 -m pytest          # 全部 49 个用例
python3 -m pytest -q tests/test_analysis.py     # 解析 + 诊断 + 语义
python3 -m pytest -q tests/test_lsp_protocol.py # 端到端 JSON-RPC
```

仅需 Python 3.10+ 标准库（`pytest` 仅用于跑测试）。

## 项目结构

```
conf_lsp/
├── __init__.py
├── __main__.py        # python -m conf_lsp
├── positions.py       # offset <-> LSP(行, UTF-16 列) 转换
├── ast_nodes.py       # AST 数据结构
├── parser.py          # 容错的 .conf 解析器
├── workspace.py       # 内存索引 / 诊断 / 解析 / hover / 引用 / 补全
├── protocol.py        # Content-Length JSON-RPC 分帧读写
└── server.py          # JSON-RPC 分发与 LSP 生命周期
tests/                 # 自动化测试（含最小 LSP 客户端）
examples/              # 跨文件 .conf 示例
```
