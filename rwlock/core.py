"""rwlock：确定性的读写锁内核（纯标准库，不读真实时钟、不起线程、不做任何 I/O）。

同一个资源上的读写锁，行为约定：

* 读读共享、读写互斥、写写互斥；
* 写优先：只要有请求在队列里等待，新来的读请求就排在队尾，不许越过等待者，
  以免源源不断的读者把排队的写者饿死；
* 先进先出：每次只从队首尝试授予，队首授不下去时后面的请求一律继续等；
* 重入：同一持有者重复申请读锁或写锁会累加计数，释放次数要与申请次数一致，
  计数归零才算真正放开；
* 升级：持有读锁者申请写锁，独占该资源时当场转换，否则保留原来的读计数排进
  队尾；升级请求随该持有者最后一个读计数的释放而作废；
* 降级：持有写锁者可以就地降为读锁，保留一个读计数并立刻推进队列；
* 超时：等待请求的 timeout 以逻辑时钟的 tick 计，时钟走到 deadline 当刻即到期，
  到期的请求从队列里作废，之后不得再被授予；
* 观察：readers() / writer() / waiting() / expired() 只读取状态，不改变状态。
"""

READ = "read"
WRITE = "write"
UPGRADE = "upgrade"


def _key(value):
    """持有者名字统一成字符串，避免同一个名字出现两种键。"""
    return value if isinstance(value, str) else str(value)


class Clock:
    """可注入的逻辑时钟：只由调用方推进，内核不读真实时间。"""

    def __init__(self, start=0):
        self._now = int(start)

    def now(self):
        """当前刻度。"""
        return self._now

    def advance(self, ticks=1):
        """推进 ticks 个刻度，返回推进后的刻度。"""
        self._now += int(ticks)
        return self._now


class LockError(Exception):
    """读写锁的用法错误。"""


class _Waiter:
    """等待队列里的一个请求：kind 取 READ / WRITE / UPGRADE。"""

    __slots__ = ("owner", "kind", "deadline", "seq")

    def __init__(self, owner, kind, deadline, seq):
        self.owner = owner
        self.kind = kind
        self.deadline = deadline
        self.seq = seq


class RWLock:
    """一个资源上的读写锁。

    参数：
        clock  可注入的逻辑时钟，等待请求的 timeout 以它的 tick 计；
               不传就新建一个从 0 开始的时钟。
    """

    def __init__(self, clock=None):
        self.clock = clock if clock is not None else Clock()
        self._readers = {}       # 持有读锁的人 -> 读计数
        self._writer = None      # 持有写锁的人，空闲时为 None
        self._write_depth = 0    # 写锁的重入计数
        self._queue = []         # 等待中的请求，按到达顺序排列
        self._expired = []       # 累计被超时作废的请求
        self._seq = 0            # 等待请求的到达序号

    # --------------------------------------------------------------- 申请

    def acquire_read(self, owner, timeout=None):
        """申请读锁。

        立即拿到返回 True；需要等待返回 False，请求按到达顺序排进队尾。
        已经持有读锁的人再申请只是把读计数加一；持有写锁的人也可以重入读锁。
        """
        owner = _key(owner)
        if self._writer == owner:
            self._readers[owner] = self._readers.get(owner, 0) + 1
            return True
        if owner in self._readers:
            self._readers[owner] += 1
            return True
        if self._pending(owner) is not None:
            return False
        if self._read_should_wait():
            self._enqueue(owner, READ, timeout)
            return False
        self._readers[owner] = 1
        return True

    def acquire_write(self, owner, timeout=None):
        """申请写锁。

        立即拿到返回 True；需要等待返回 False，请求按到达顺序排进队尾。
        持有读锁者申请写锁是升级：独占该资源时就地转换，否则排进队尾；
        持有写锁者再申请只是把重入计数加一。
        """
        owner = _key(owner)
        if self._writer == owner:
            self._write_depth += 1
            return True
        if owner in self._readers:
            if self._writer is None:
                self._readers.pop(owner)
                self._writer = owner
                self._write_depth = 1
                return True
            self._readers.pop(owner)
            self._enqueue(owner, UPGRADE, timeout)
            return False
        if self._pending(owner) is not None:
            return False
        if self._writer is not None or self._readers or self._queue:
            self._enqueue(owner, WRITE, timeout)
            return False
        self._writer = owner
        self._write_depth = 1
        return True

    # ----------------------------------------------------------- 释放与降级

    def release_read(self, owner):
        """释放一次读锁，返回剩余的读计数（归零表示完全放开）。"""
        owner = _key(owner)
        if owner not in self._readers:
            raise LockError("持有者 %s 没有持有该资源的读锁" % (owner,))
        del self._readers[owner]
        self._drop_pending(owner)
        self._dispatch()
        return 0

    def release_write(self, owner):
        """释放一次写锁，返回剩余的写计数（归零表示完全放开）。"""
        owner = _key(owner)
        if self._writer != owner:
            raise LockError("持有者 %s 没有持有该资源的写锁" % (owner,))
        self._writer = None
        self._write_depth = 0
        self._dispatch()
        return 0

    def downgrade(self, owner):
        """把写锁就地降为读锁，返回保留的读计数；降级后立刻推进队列。"""
        owner = _key(owner)
        if self._writer != owner:
            raise LockError("持有者 %s 没有持有该资源的写锁" % (owner,))
        self._writer = None
        self._write_depth = 0
        self._dispatch()
        return self._readers.get(owner, 0)

    # --------------------------------------------------------------- 时钟

    def advance(self, ticks=1):
        """推进逻辑时钟，作废到期的等待请求，返回 [(持有者, 类型), ...]。

        到期的请求从队列里摘掉，之后不得再被授予；摘掉后会顺手推进一次队列。
        """
        self.clock.advance(ticks)
        now = self.clock.now()
        expired = []
        while self._queue:
            head = self._queue[0]
            if head.deadline is None or head.deadline > now:
                break
            waiter = self._queue.pop(0)
            expired.append((waiter.owner, waiter.kind))
        if expired:
            self._expired.extend(expired)
            self._dispatch()
        return expired

    # --------------------------------------------------------------- 观察

    def readers(self):
        """当前的读者 {持有者: 读计数}，按名字排序。"""
        return {owner: self._readers[owner] for owner in sorted(self._readers)}

    def read_depth(self, owner):
        """该持有者的读计数；没有持有读锁时为 0。"""
        return self._readers.get(_key(owner), 0)

    def writer(self):
        """当前的写者；空闲时为 None。"""
        return self._writer

    def write_depth(self):
        """当前写锁的重入计数；空闲时为 0。"""
        return self._write_depth

    def waiting(self):
        """等待队列 [(持有者, 请求类型), ...]，按到达顺序。"""
        return [(waiter.owner, waiter.kind) for waiter in self._queue]

    def expired(self):
        """累计被超时作废的请求 [(持有者, 请求类型), ...]。"""
        return list(self._expired)

    # --------------------------------------------------------------- 内部

    def _pending(self, owner):
        """该持有者当前排队中的请求，没有则 None。"""
        for waiter in self._queue:
            if waiter.owner == owner:
                return waiter
        return None

    def _drop_pending(self, owner):
        """撤销该持有者排队中的请求，返回是否真的撤掉了什么。"""
        kept = [waiter for waiter in self._queue if waiter.owner != owner]
        if len(kept) == len(self._queue):
            return False
        self._queue = kept
        return True

    def _read_should_wait(self):
        """这次读请求是否必须排队：写者正持有锁的时候要排队。"""
        return self._writer is not None

    def _enqueue(self, owner, kind, timeout):
        """把请求按到达顺序放进队列，并记下它的到期刻度。"""
        self._seq += 1
        deadline = None if timeout is None else self.clock.now() + int(timeout)
        self._queue.append(_Waiter(owner, kind, deadline, self._seq))

    def _dispatch(self):
        """试着把队列里的请求授予出去，返回本次授予的 (持有者, 类型) 列表。"""
        granted = []
        remaining = []
        for waiter in self._queue:
            if waiter.kind == READ and self._writer is None:
                self._readers[waiter.owner] = self._readers.get(waiter.owner, 0) + 1
                granted.append((waiter.owner, READ))
            elif waiter.kind == WRITE and self._writer is None and not self._readers:
                self._writer = waiter.owner
                self._write_depth = 1
                granted.append((waiter.owner, WRITE))
            elif (waiter.kind == UPGRADE and self._writer is None
                    and set(self._readers) == {waiter.owner}):
                del self._readers[waiter.owner]
                self._writer = waiter.owner
                self._write_depth = 1
                granted.append((waiter.owner, WRITE))
            else:
                remaining.append(waiter)
        self._queue = remaining
        return granted
