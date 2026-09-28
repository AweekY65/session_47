# scls — Simple Configuration Language Server

`scls` 是一个面向简单配置语言（SCL）的**纯本地** Language Server。所有源码、
符号索引和诊断状态只保存在内存或本地文件中：不连接 VS Code Marketplace、
远程语言服务、数据库或任何外部服务。仅依赖 Python 3.8+ 标准库，无需安装
任何第三方包，也不需要打开 IDE。

## 语言语法（`.scl`）

```scl
# 注释（# 或 ; 开头）
[section]                # section 头部

key = "string"           # 字符串（支持 \" \\ \n \t 转义）
count = 42               # 数字（整数或浮点，支持负数）
enabled = true           # 布尔（true / false）
mirror = ${section.key}  # 引用表达式，可跨文件解析

name: string = "x"       # 可选类型注解：string | number | bool
```

键值上方的注释行会作为该键的说明文档，在 hover 中显示。

## 启动方式

服务器通过 stdin/stdout 使用 LSP 基础协议（`Content-Length` 头 + JSON-RPC)
通信：

```bash
python3 -m scls
```

任何能讲 LSP 的客户端都可以连接它；也可以直接用终端管道发送消息，例如：

```bash
{ printf 'Content-Length: 90\r\n\r\n%s' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"rootUri":null,"capabilities":{}}}'; \
  sleep 1; } | python3 -m scls
```

`initialize` 时若提供 `rootUri`（或 `workspaceFolders`)，服务器会递归索引该
目录下的所有本地 `*.scl` 文件，用于跨文件跳转与引用解析。

## 支持的 LSP 能力

| 能力 | 方法 | 说明 |
| --- | --- | --- |
| 文档同步 | `textDocument/didOpen` / `didChange` / `didClose` | 增量同步（`TextDocumentSyncKind.Incremental`)，局部修改无需重启服务；每次变更后重新发布所有文档诊断，已修复的错误立即消失 |
| 诊断 | `textDocument/publishDiagnostics` | 语法错误、重复 key（含跨文件）、未知引用、类型不匹配（注解 vs 字面量 / 注解 vs 引用目标类型，引用链可传递），均带准确的 0 起始行列范围 |
| 补全 | `textDocument/completion` | 按上下文给出候选：key 位置 → 当前 section 在工作区中已有的 key;`key:` 后 → 类型关键字；`=` 后 → `true`/`false`/引用片段；`${` 内 → `section.key` 引用；`${section.` 后 → 该 section 的 key |
| 跳转定义 | `textDocument/definition` | 在引用或定义处跳转到符号定义，支持跨文件 |
| 查找引用 | `textDocument/references` | 返回定义（可选）与所有 `${section.key}` 引用位置，跨文件 |
| 悬停 | `textDocument/hover` | 显示符号类型、来源文件与行号、当前值和文档注释；未知符号安全返回 `null` |

## 项目结构

```
scls/
  __main__.py   # 入口：python3 -m scls
  parser.py     # 词法/语法分析，错误恢复，精确位置
  analyzer.py   # 工作区索引、符号表、诊断、引用解析
  server.py     # JSON-RPC/LSP 协议与请求处理
tests/
  test_parser.py  # parser 与 analyzer 单元测试
  test_server.py  # 端到端测试：子进程 + 真实 JSON-RPC 消息
examples/         # 示例 .scl 文件
```

## 运行测试

测试通过终端直接向服务器子进程发送 JSON-RPC 消息，覆盖解析、诊断、补全、
跨文件跳转、引用搜索、悬停和增量修改等场景，不需要 VS Code 或任何 GUI:

```bash
python3 -m unittest discover -s tests -v
```
