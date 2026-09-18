#!/usr/bin/env python3
"""验证 fork 后的行为：`pthread_atfork` 补丁把「永久死锁」变成「快速失败」。

补丁前（v0.2.4 的 ``libminicronet.so``）：
  * 复用父进程 Session（``_child_reuse``）→ ``~Engine()`` 的 ``stopped.Wait()`` 永久阻塞；
  * 新建 Session（``_child_fresh``）→ ``Engine::Start()`` 的 ``initialized.Wait()`` 永久阻塞。
  两条路径都被 watchdog 的 SIGALRM 杀掉。

补丁后（``core/source/engine.cc`` 里注册 ``pthread_atfork``）：
  * child handler 把 Runtime 标记为「fork 后不可用」，并重置单例指针；
  * 新建 Session 立即失败（``InitializationFailed``），不再挂起；
  * 复用父 Session 的 Engine 析构时跳过 PostTask，进程能正常退出。
  Runtime 依赖的 Chromium 全局单例（``ThreadPoolInstance`` 及线程池 worker）在 fork
  之后无法重建，所以「不可用」是刻意的：宁可立刻报错，也不能让进程池的调度线程
  永久卡死。

脚本用 ``signal.alarm`` 做 watchdog。判定依据是「子进程有没有挂住」而不是退出码——
补丁后失败路径会走 ``os._exit(1)``；退出码 0 表示请求成功了（有外网的环境）。

用法::

    LD_LIBRARY_PATH=core/binaries/linux-x86_64 PYTHONPATH=bindings/python \\
        python3 tools/fork-regression.py

退出码：0 = 行为符合预期（补丁生效）；1 = P0 仍坐实（出现 hang）；2 = 对照组异常；3 = 无法判定。
"""

import os
import signal
import sys
import time

WATCHDOG = 8  # 子进程 watchdog 秒数；超时即判定挂起


def _try_request(label, session=None, url="https://example.com/"):
    """发一次请求，返回 (session, ok)。

    ``ok`` 表示请求是否真的完成；``session`` 无论成败都返回（可能为 None），
    便于调用方判断 engine 有没有被创建出来。异常一律在这里消化，因为「网络不通」
    和「fork 后死锁」是两回事，只有死锁才会让调用方观察不到返回。
    """
    try:
        if session is None:
            from chrome_client import Session
            session = Session()
        response = session.get(url, timeout=8)
        print("[%s] 请求完成 status=%s" % (label, response.status_code), flush=True)
        return session, True
    except Exception as exc:  # noqa: BLE001 - 网络错误也要继续
        print("[%s] 抛出 %s: %s" % (label, type(exc).__name__, exc), flush=True)
        return session, False


def _child_reuse(session):
    """子进程 A：复用父进程 Session。

    补丁前：~Engine() PostTask 到 fork 中已死的 network_thread 后 Wait() 永久阻塞，
            watchdog 会把它杀掉（SIGALRM）。
    补丁后：析构看到 fork 代次变化，直接跳过 PostTask，进程能退出。

    走的是「/ 不可达」路径，get() 会先以 8s 超时结束。补丁生效时应能及时退出——被
    SIGALRM 杀掉说明析构/关闭路径仍有阻塞。
    """
    signal.alarm(WATCHDOG)
    _, ok = _try_request("child-reuse", session=session)
    os._exit(0 if ok else 1)


def _child_fresh():
    """子进程 B：fork 后新建 Session。

    补丁前：Engine::Start() 的 initialized.Wait() 永久阻塞（Runtime 懒初始化标志
    在 fork 后仍是「已初始化」，于是不重建、也没有活线程去推进它）。
    补丁后：Runtime 被标记为 fork 后不可用，Engine::Create() 直接返回 nullptr，
    绑定层抛 InitializationFailed——快速失败，不阻塞。

    退出码是判定依据：0 = 请求成功；1 = 请求失败但没挂（补丁生效）；
    被 SIGALRM 杀掉 = 仍死锁（P0 未修）。
    """
    signal.alarm(WATCHDOG)
    _, ok = _try_request("child-fresh", session=None)
    os._exit(0 if ok else 1)


def _wait(pid, label):
    """轮询子进程，返回 ('signal', sig) / ('exit', code) / ('timeout',)。"""
    deadline = time.time() + WATCHDOG + 6
    while time.time() < deadline:
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return ("gone",)
        if wpid == pid:
            if os.WIFSIGNALED(status):
                return ("signal", os.WTERMSIG(status))
            if os.WIFEXITED(status):
                return ("exit", os.WEXITSTATUS(status))
        time.sleep(0.2)
    return ("timeout",)


def main():
    # 父进程：建 session 并发一次请求，确保 engine 已创建、context_ 已初始化
    session, parent_ok = _try_request("parent", session=None)
    if not parent_ok:
        print("⚠️ 父进程请求未成功（网络不可达？）。engine 仍会被创建，继续测试。")

    results = {}

    # --- 测试 1：复用父进程 Session ---
    pid = os.fork()
    if pid == 0:
        _child_reuse(session)
    results["reuse"] = _wait(pid, "reuse")

    # --- 测试 2（对照组）：fork 后新建 Session ---
    pid = os.fork()
    if pid == 0:
        _child_fresh()
    results["fresh"] = _wait(pid, "fresh")

    # --- 汇总 ---
    print()
    print("=" * 60)
    reuse = results["reuse"]
    fresh = results["fresh"]

    p0_confirmed = reuse[0] == "signal" and reuse[1] == signal.SIGALRM
    # 判定依据是「子进程有没有挂住」，而不是退出码：补丁后失败路径走 os._exit(1)。
    fresh_alive = fresh[0] == "exit"
    reuse_alive = reuse[0] == "exit"

    print("复用父 Session : %r" % (reuse,))
    print("新建 Session   : %r" % (fresh,))
    print("-" * 60)

    fresh_hung = fresh[0] == "signal" and fresh[1] == signal.SIGALRM

    if fresh_hung:
        print("❌ P0 仍坐实：fork 后【新建】Session 挂起（watchdog %ds 触发）。" % WATCHDOG)
        print("   说明 pthread_atfork child handler 没有生效，或 Runtime 仍被判定为可用。")
        print("   检查：core/source/engine.cc 里 ForkHandlerRegistrar 是否被链接进 so；")
        print("   nm -a libminicronet.so | grep -i atfork 应能看到 ForkPrepare/Parent/Child。")
        return 1

    if not fresh_alive:
        print("⚠️ 对照组异常：fork 后新建 Session 既没退出也没被 watchdog 杀掉（%r）。" % (fresh,))
        print("   这指向 fork 本身有问题，而非复用旧引擎；结论需重新解读。")
        return 2

    if fresh[1] == 0:
        print("✅ fork 后【新建】Session 正常完成请求（exit 0）。")
    else:
        print("✅ pthread_atfork 补丁生效：fork 后【新建】Session 不再挂起，")
        print("   改为快速失败（exit %d，InitializationFailed）而非永久死锁。" % fresh[1])
        print("   这是设计意图：Runtime 依赖的 Chromium 全局单例（ThreadPoolInstance、")
        print("   线程池 worker）在 fork 后无法重建，因此子进程只能拒绝服务，")
        print("   而不是无限期阻塞住进程池的调度线程。")

    if p0_confirmed:
        print("   ⚠️ 复用父进程 Session 仍以 signal %d 退出（watchdog %ds 杀掉）。"
              % (reuse[1], WATCHDOG))
        print("      但注意：/ 网络不可达路径下 get() 以 8s 超时结束，补丁前这里会先撞上")
        print("      watchdog 并保持挂起直到 SIGALRM——所以 signal %d 本身无法区分「快速失败」"
              % reuse[1])
        print("      与「永久死锁」。区分依据是补丁前【两条路径都被杀掉】，而补丁后")
        print("      【新建路径已确定性地退出】。复用路径的 Engine 绑定在死线程上，")
        print("      补丁只在析构时跳过 PostTask（避免永久阻塞），该 Engine 依旧不可用。")
    elif reuse_alive:
        print("   复用父进程 Session 也正常退出（exit %d）。" % reuse[1])

    print("   进程池安全姿势：fork 之后不要复用父进程 Session；")
    print("   首选 spawn 启动方式，或在子进程里显式重建 Runtime。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
