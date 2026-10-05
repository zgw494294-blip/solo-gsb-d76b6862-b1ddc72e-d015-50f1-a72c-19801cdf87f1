"""Flask 主进程：管理全站唯一的一次逐行运行。

- 每次运行启动一个独立子进程（tracer.py），先停在首条待执行行。
- 控制请求必须带 run_id；step / continue 还必须带 pause_seq。
- 同一次停顿最多被消费一次，重复或过期请求一律 409 且不推进。
- 任意时刻（包括暂停与死循环）都可查询状态、终止运行；终止会回收子进程。
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

MAX_LINES = 80
TRACER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tracer.py")
TERMINAL_STATUSES = {"finished", "exception", "terminated"}
STEP_WAIT_SECONDS = 5


class Run:
    def __init__(self, run_id, proc, source_path):
        self.id = run_id
        self.proc = proc
        self.source_path = source_path
        self.pause_seq = 0          # 停顿序号：每次暂停 +1
        self.status = "starting"    # starting/paused/running/finished/exception/terminated
        self.line = None            # 当前待执行行
        self.locals = {}
        self.stdout = ""
        self.exception = None
        self.consumed = False       # 当前停顿是否已被消费
        self.killed = False
        self.got_terminal_event = False
        self.cond = threading.Condition()

    def snapshot(self):
        return {
            "run_id": self.id,
            "status": self.status,
            "pause_seq": self.pause_seq,
            "line": self.line,
            "locals": self.locals,
            "stdout": self.stdout,
            "exception": self.exception,
        }


state_lock = threading.Lock()
current_run = None
run_counter = 0


def tracer_reader(run):
    """后台线程：读取子进程协议事件，更新运行状态，结束后回收子进程。"""
    proc = run.proc
    try:
        for raw in proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            with run.cond:
                if run.status in TERMINAL_STATUSES:
                    continue
                kind = event.get("type")
                if kind == "paused":
                    run.pause_seq += 1
                    run.status = "paused"
                    run.line = event.get("line")
                    run.locals = event.get("locals") or {}
                    run.consumed = False
                elif kind == "output":
                    run.stdout += event.get("data", "")
                elif kind == "exception":
                    run.status = "exception"
                    run.exception = event.get("message")
                    run.line = event.get("line")
                    run.got_terminal_event = True
                elif kind == "finished":
                    run.status = "finished"
                    run.got_terminal_event = True
                run.cond.notify_all()
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        with run.cond:
            if run.status not in TERMINAL_STATUSES:
                if run.killed:
                    run.status = "terminated"
                else:
                    run.status = "finished"
                    if not run.got_terminal_event:
                        run.exception = "子进程意外退出"
            run.cond.notify_all()
        try:
            os.unlink(run.source_path)
        except OSError:
            pass


def count_lines(code):
    lines = code.split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    return len(lines)


@app.get("/")
def index():
    return render_template("index.html", max_lines=MAX_LINES)


@app.post("/api/run")
def api_run():
    global current_run, run_counter
    data = request.get_json(silent=True) or {}
    code = data.get("code")
    if not isinstance(code, str) or not code.strip():
        return jsonify({"error": "代码不能为空"}), 400
    if count_lines(code) > MAX_LINES:
        return jsonify({"error": "代码最多 %d 行" % MAX_LINES}), 400

    with state_lock:
        if current_run is not None and current_run.status not in TERMINAL_STATUSES:
            return jsonify({
                "error": "已有未结束的运行（run_id=%s），请先终止或等待结束" % current_run.id,
                "run_id": current_run.id,
            }), 409
        run_counter += 1
        fd, path = tempfile.mkstemp(suffix=".py", prefix="usercode_")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(code)
        proc = subprocess.Popen(
            [sys.executable, TRACER_PATH, path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,  # 独立进程组，便于整组终止回收
        )
        run = Run(run_counter, proc, path)
        current_run = run
        threading.Thread(target=tracer_reader, args=(run,), daemon=True).start()

    # 等待首次暂停（或直接结束/异常），让响应里就带上首个停顿。
    with run.cond:
        run.cond.wait_for(lambda: run.status != "starting", timeout=STEP_WAIT_SECONDS)
        return jsonify(run.snapshot())


def control(command):
    data = request.get_json(silent=True) or {}
    run_id = data.get("run_id")
    pause_seq = data.get("pause_seq")
    run = current_run
    if run is None or run.id != run_id:
        return jsonify({
            "error": "运行不存在或已过期",
            "current_run_id": run.id if run else None,
        }), 409
    with run.cond:
        if run.status != "paused":
            return jsonify({"error": "当前不在暂停状态", "state": run.snapshot()}), 409
        if pause_seq != run.pause_seq or run.consumed:
            return jsonify({
                "error": "停顿序号过期或该停顿已被消费，未推进",
                "state": run.snapshot(),
            }), 409
        run.consumed = True
        run.status = "running"
        try:
            run.proc.stdin.write(json.dumps({"cmd": command}) + "\n")
            run.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        if command == "step":
            # 短暂等待下一停顿；长耗时行则由前端轮询拿到结果。
            run.cond.wait_for(lambda: run.status != "running",
                              timeout=STEP_WAIT_SECONDS)
        return jsonify(run.snapshot())


@app.post("/api/step")
def api_step():
    return control("step")


@app.post("/api/continue")
def api_continue():
    return control("continue")


@app.post("/api/terminate")
def api_terminate():
    data = request.get_json(silent=True) or {}
    run_id = data.get("run_id")
    run = current_run
    if run is None or run.id != run_id:
        return jsonify({
            "error": "运行不存在或已过期",
            "current_run_id": run.id if run else None,
        }), 409
    with run.cond:
        if run.status in TERMINAL_STATUSES:
            return jsonify(run.snapshot())
        run.status = "terminated"
        run.killed = True
        run.consumed = True
        run.cond.notify_all()
    try:
        os.killpg(run.proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        run.proc.wait(timeout=5)  # 回收子进程
    except subprocess.TimeoutExpired:
        pass
    return jsonify(run.snapshot())


@app.get("/api/state")
def api_state():
    run_id = request.args.get("run_id", type=int)
    run = current_run
    if run is None or (run_id is not None and run.id != run_id):
        return jsonify({
            "error": "运行不存在或已过期",
            "current_run_id": run.id if run else None,
        }), 404
    with run.cond:
        return jsonify(run.snapshot())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
