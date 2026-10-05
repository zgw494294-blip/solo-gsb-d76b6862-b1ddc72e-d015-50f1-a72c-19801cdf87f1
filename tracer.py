"""逐行执行追踪器。

由 app.py 以独立子进程方式启动：
    python tracer.py <用户源码文件路径>

协议（子进程 stdout，每行一个 JSON 对象）：
    {"type": "paused",    "line": N, "locals": {...}}   暂停在第 N 行（尚未执行）
    {"type": "output",    "data": "..."}                用户代码的标准输出/标准错误增量
    {"type": "exception", "message": "...", "line": N}  用户代码抛出未捕获异常
    {"type": "finished"}                                正常执行完毕

命令（子进程 stdin，每行一个 JSON 对象）：
    {"cmd": "step"}      执行到下一行并再次暂停
    {"cmd": "continue"}  不再暂停，直接跑到结束
stdin 关闭（EOF）时自行退出；终止运行由主进程直接 kill 本进程完成。
"""

import io
import json
import os
import sys
import traceback

USER_FILENAME = "<user_code>"
MAX_REPR_LEN = 200


def emit(obj):
    # 协议走真实 stdout（sys.__stdout__），用户代码的 sys.stdout 已被替换。
    sys.__stdout__.write(json.dumps(obj, ensure_ascii=True) + "\n")
    sys.__stdout__.flush()


class OutputStream(io.TextIOBase):
    """把用户代码的 print 等输出实时转成 output 事件。"""

    def write(self, data):
        if data:
            emit({"type": "output", "data": data})
        return len(data)

    def flush(self):
        pass


def safe_repr(value):
    try:
        text = repr(value)
    except Exception:
        return "<repr() 出错>"
    if len(text) > MAX_REPR_LEN:
        text = text[:MAX_REPR_LEN] + " ..."
    return text


def snapshot_locals(frame):
    result = {}
    for name, value in frame.f_locals.items():
        if name.startswith("__") and name.endswith("__"):
            continue
        result[name] = safe_repr(value)
    return result


def read_command():
    line = sys.stdin.readline()
    if not line:  # 主进程关闭了管道
        return "terminate"
    try:
        return json.loads(line).get("cmd", "terminate")
    except ValueError:
        return "terminate"


step_mode = {"enabled": True}


def trace_lines(frame, event, arg):
    # 只追踪用户源码，不进入标准库等其它文件。
    if frame.f_code.co_filename != USER_FILENAME:
        return None
    if event == "line":
        if not step_mode["enabled"]:
            return None
        emit({
            "type": "paused",
            "line": frame.f_lineno,
            "locals": snapshot_locals(frame),
        })
        command = read_command()
        if command == "step":
            return trace_lines
        if command == "continue":
            step_mode["enabled"] = False
            return None
        os._exit(0)  # terminate / 非法命令
    return trace_lines


def main():
    source_path = sys.argv[1]
    with open(source_path, "r", encoding="utf-8") as handle:
        source = handle.read()

    sys.stdout = OutputStream()
    sys.stderr = OutputStream()

    # 不支持交互输入：调用 input() 直接报错，避免误读命令管道。
    import builtins
    def no_input(prompt=""):
        raise RuntimeError("本平台不支持交互式 input()")
    builtins.input = no_input

    try:
        code = compile(source, USER_FILENAME, "exec")
    except SyntaxError as exc:
        emit({
            "type": "exception",
            "message": "SyntaxError: %s" % exc,
            "line": exc.lineno,
        })
        return

    failed = False
    sys.settrace(trace_lines)
    try:
        exec(code, {"__name__": "__main__"})
    except SystemExit:
        pass
    except BaseException:
        failed = True
        exc_type, exc_value, tb = sys.exc_info()
        line_no = None
        for summary in traceback.extract_tb(tb):
            if summary.filename == USER_FILENAME:
                line_no = summary.lineno
        emit({
            "type": "exception",
            "message": "".join(
                traceback.format_exception_only(exc_type, exc_value)
            ).strip(),
            "line": line_no,
        })
    finally:
        sys.settrace(None)

    if not failed:
        emit({"type": "finished"})


if __name__ == "__main__":
    main()
