# Python 逐行执行观察器

在网页上逐行观察本机可信 Python 代码的实际执行：Flask + 原生 HTML/CSS/JavaScript，
每次运行都在独立子进程中通过 `sys.settrace` 追踪，先停在首条待执行行。

## 启动

一条命令（需要 Docker 与 Docker Compose）：

```bash
docker compose up --build
```

访问地址：<http://localhost:5000>（端口 **5000**）

也可以不用 Docker 本地运行：

```bash
pip install -r requirements.txt
python app.py
```

## 功能

- 输入最多 **80 行** Python 代码，点击「运行」后在独立子进程中执行，并停在首条待执行行。
- **单步**：停在下一条实际将执行的源码行（会进入函数调用内部，循环每轮迭代都会暂停）。
- **继续到结束**：不再暂停，直接运行至结束（死循环时输出仍实时可见）。
- **终止**：随时结束运行并回收子进程。
- 页面实时显示：待执行行（高亮）、当前局部变量的文本值、标准输出、未捕获异常。
- 运行期间源码只读；结束后可改稿重跑。
- 页面内置四个示例：赋值、函数调用、循环、异常。

## 规则与限制

- 全站同时只允许一个未结束的运行；有运行进行中时再次「运行」会被拒绝（409）。
- 控制请求（单步 / 继续 / 终止）都携带 `run_id`；单步和继续还携带停顿序号 `pause_seq`。
  同一次停顿最多被接受一次，重复或过期的请求返回 409 且不会推进执行。
- 旧运行的迟到响应（`run_id` 不匹配）在服务端被拒绝、在前端被丢弃，不会覆盖新运行。
- 暂停中或死循环中仍可查询状态（`GET /api/state`）与终止；终止通过进程组 SIGKILL + `wait()` 回收子进程。
- 被观察的代码不支持交互输入（`input()` 会抛异常）与第三方包（仅标准库）。
- 暂停粒度与 CPython 行事件一致（行号变化时触发）：`while True: pass` 这类写在同一行的
  循环单步后不会再产生新的暂停（表现为"运行中"），但随时可查询、可终止；多行循环每轮迭代都会暂停。
- 本平台面向**本机可信代码**，子进程不做沙箱隔离，请勿粘贴不可信代码。

## API 简介

| 方法 | 路径 | 请求体 | 说明 |
| --- | --- | --- | --- |
| POST | `/api/run` | `{code}` | 启动运行，返回首个停顿的状态 |
| POST | `/api/step` | `{run_id, pause_seq}` | 推进到下一行 |
| POST | `/api/continue` | `{run_id, pause_seq}` | 继续到结束 |
| POST | `/api/terminate` | `{run_id}` | 终止并回收子进程 |
| GET | `/api/state?run_id=N` | — | 查询当前状态 |

状态对象：`{run_id, status, pause_seq, line, locals, stdout, exception}`，
其中 `status` 为 `starting / paused / running / finished / exception / terminated`。

## 文件结构

```
app.py               Flask 主进程（运行管理、并发与序号校验、子进程回收）
tracer.py            子进程追踪器（settrace 逐行暂停，JSON 行协议通信）
templates/index.html 页面
static/app.js        前端逻辑（轮询、高亮、run_id 校验）
static/style.css     样式
Dockerfile / docker-compose.yml
```
