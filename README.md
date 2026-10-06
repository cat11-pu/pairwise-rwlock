# rwlock

一个纯内存、确定性的读写锁内核：读读共享、读写互斥、写优先的等待队列、读锁升级与
写锁降级、读写的重入计数，以及按逻辑时钟计的超时作废。时间由调用方以整数刻度注入，
内核不读真实时钟、不起线程，也不做任何 I/O。

## 目录

- `rwlock/core.py`：内核实现（读写状态、等待队列、升级与降级、重入计数、超时）
- `tests/test_core.py`：内核的行为测试

## 怎么跑测试

在项目根目录执行：

    python3 -m unittest discover -s tests -v

Windows 上把 `python3` 换成你的解释器路径，例如：

    C:/Users/<你>/AppData/Local/Programs/Python/Python313/python.exe -m unittest discover -s tests -v
