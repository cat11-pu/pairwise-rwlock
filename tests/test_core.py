"""rwlock.core 的行为测试：读共享写互斥、写优先、升级与降级、重入计数、超时。"""

import unittest

from rwlock.core import LockError, RWLock


class RWLockCoreTest(unittest.TestCase):
    """覆盖正常路径、边界输入、异常路径与不变量。"""

    def setUp(self):
        self.lock = RWLock()

    def test_readers_share_and_writer_waits_for_the_last_reader(self):
        """读锁可以共享，写者要等到最后一个读者释放才进得来。"""
        self.assertTrue(self.lock.acquire_read("r1"))
        self.assertTrue(self.lock.acquire_read("r2"))
        self.assertEqual(self.lock.readers(), {"r1": 1, "r2": 1})
        self.assertIsNone(self.lock.writer())
        self.assertFalse(self.lock.acquire_write("w"))
        self.assertIsNone(self.lock.writer())
        self.assertEqual(self.lock.waiting(), [("w", "write")])
        self.assertEqual(self.lock.read_depth("w"), 0)
        self.assertEqual(self.lock.release_read("r1"), 0)
        self.assertEqual(self.lock.readers(), {"r2": 1})
        self.assertIsNone(self.lock.writer())
        self.assertEqual(self.lock.release_read("r2"), 0)
        self.assertEqual(self.lock.writer(), "w")
        self.assertEqual(self.lock.write_depth(), 1)
        self.assertEqual(self.lock.waiting(), [])
        self.assertEqual(self.lock.release_write("w"), 0)
        self.assertIsNone(self.lock.writer())
        self.assertEqual(self.lock.write_depth(), 0)

    def test_new_reader_queues_behind_a_waiting_writer(self):
        """有人在排队等写锁时，新来的读请求必须排在它后面。"""
        self.assertTrue(self.lock.acquire_read("r1"))
        self.assertFalse(self.lock.acquire_write("w"))
        self.assertEqual(self.lock.waiting(), [("w", "write")])
        self.assertFalse(self.lock.acquire_read("r2"))
        self.assertEqual(self.lock.waiting(), [("w", "write"), ("r2", "read")])
        self.assertEqual(self.lock.read_depth("r2"), 0)
        self.assertEqual(self.lock.readers(), {"r1": 1})
        self.assertEqual(self.lock.release_read("r1"), 0)
        self.assertEqual(self.lock.writer(), "w")
        self.assertEqual(self.lock.read_depth("r2"), 0)
        self.assertEqual(self.lock.waiting(), [("r2", "read")])
        self.assertEqual(self.lock.release_write("w"), 0)
        self.assertEqual(self.lock.read_depth("r2"), 1)
        self.assertEqual(self.lock.waiting(), [])

    def test_queued_writer_is_not_overrun_by_later_readers(self):
        """队列里排着写者时，后面的读请求一次也不能越过它先拿到锁。"""
        self.assertTrue(self.lock.acquire_read("r1"))
        self.assertTrue(self.lock.acquire_read("r2"))
        self.assertFalse(self.lock.acquire_write("w"))
        self.assertFalse(self.lock.acquire_read("r3"))
        self.assertEqual(self.lock.waiting(), [("w", "write"), ("r3", "read")])
        self.assertEqual(self.lock.release_read("r1"), 0)
        self.assertIsNone(self.lock.writer())
        self.assertEqual(self.lock.read_depth("r3"), 0)
        self.assertEqual(self.lock.waiting(), [("w", "write"), ("r3", "read")])
        self.assertEqual(self.lock.release_read("r2"), 0)
        self.assertEqual(self.lock.writer(), "w")
        self.assertEqual(self.lock.read_depth("r3"), 0)
        self.assertEqual(self.lock.release_write("w"), 0)
        self.assertEqual(self.lock.read_depth("r3"), 1)
        self.assertEqual(self.lock.waiting(), [])

    def test_reentrant_read_releases_one_count_at_a_time(self):
        """同一个持有者重复申请读锁要按次数逐个释放。"""
        self.assertTrue(self.lock.acquire_read("r1"))
        self.assertTrue(self.lock.acquire_read("r1"))
        self.assertEqual(self.lock.read_depth("r1"), 2)
        self.assertEqual(self.lock.readers(), {"r1": 2})
        self.assertFalse(self.lock.acquire_write("w"))
        self.assertEqual(self.lock.waiting(), [("w", "write")])
        self.assertEqual(self.lock.release_read("r1"), 1)
        self.assertEqual(self.lock.read_depth("r1"), 1)
        self.assertIsNone(self.lock.writer())
        self.assertEqual(self.lock.read_depth("w"), 0)
        self.assertEqual(self.lock.release_read("r1"), 0)
        self.assertEqual(self.lock.readers(), {})
        self.assertEqual(self.lock.writer(), "w")

    def test_reentrant_write_releases_one_count_at_a_time(self):
        """重入的写锁也要按次数释放，计数归零前别人进不来。"""
        self.assertTrue(self.lock.acquire_write("w"))
        self.assertTrue(self.lock.acquire_write("w"))
        self.assertEqual(self.lock.write_depth(), 2)
        self.assertFalse(self.lock.acquire_read("r1"))
        self.assertEqual(self.lock.waiting(), [("r1", "read")])
        self.assertEqual(self.lock.release_write("w"), 1)
        self.assertEqual(self.lock.writer(), "w")
        self.assertEqual(self.lock.write_depth(), 1)
        self.assertEqual(self.lock.read_depth("r1"), 0)
        self.assertEqual(self.lock.release_write("w"), 0)
        self.assertIsNone(self.lock.writer())
        self.assertEqual(self.lock.read_depth("r1"), 1)
        self.assertEqual(self.lock.waiting(), [])

    def test_upgrade_succeeds_only_for_the_only_reader(self):
        """还有别的读者时，升级请求必须排队，原来的读计数一个都不许少。"""
        self.assertTrue(self.lock.acquire_read("r1"))
        self.assertTrue(self.lock.acquire_read("r2"))
        self.assertFalse(self.lock.acquire_write("r1"))
        self.assertIsNone(self.lock.writer())
        self.assertEqual(self.lock.readers(), {"r1": 1, "r2": 1})
        self.assertEqual(self.lock.read_depth("r1"), 1)
        self.assertEqual(self.lock.waiting(), [("r1", "upgrade")])
        self.assertEqual(self.lock.release_read("r2"), 0)
        self.assertEqual(self.lock.writer(), "r1")
        self.assertEqual(self.lock.readers(), {})
        self.assertEqual(self.lock.read_depth("r1"), 0)
        self.assertEqual(self.lock.write_depth(), 1)
        self.assertEqual(self.lock.waiting(), [])
        self.assertEqual(self.lock.release_write("r1"), 0)
        self.assertIsNone(self.lock.writer())

    def test_blocked_upgrade_keeps_read_count_and_is_dropped_on_release(self):
        """升级拿不到时保留读计数排队；释放最后一个读计数后升级请求作废。"""
        self.assertTrue(self.lock.acquire_read("r1"))
        self.assertFalse(self.lock.acquire_write("w"))
        self.assertFalse(self.lock.acquire_write("r1"))
        self.assertEqual(self.lock.read_depth("r1"), 1)
        self.assertEqual(self.lock.readers(), {"r1": 1})
        self.assertIsNone(self.lock.writer())
        self.assertEqual(self.lock.waiting(), [("w", "write"), ("r1", "upgrade")])
        self.assertEqual(self.lock.release_read("r1"), 0)
        self.assertEqual(self.lock.waiting(), [])
        self.assertEqual(self.lock.writer(), "w")
        self.assertEqual(self.lock.read_depth("r1"), 0)
        self.assertEqual(self.lock.release_write("w"), 0)
        self.assertIsNone(self.lock.writer())

    def test_downgrade_keeps_one_read_count_and_advances_the_queue(self):
        """写锁降级为读锁后保留一个读计数，等在后面的读者立刻被放行。"""
        self.assertTrue(self.lock.acquire_write("w"))
        self.assertFalse(self.lock.acquire_read("r1"))
        self.assertFalse(self.lock.acquire_write("w2"))
        self.assertEqual(self.lock.waiting(), [("r1", "read"), ("w2", "write")])
        self.assertEqual(self.lock.downgrade("w"), 1)
        self.assertIsNone(self.lock.writer())
        self.assertEqual(self.lock.read_depth("w"), 1)
        self.assertEqual(self.lock.readers(), {"r1": 1, "w": 1})
        self.assertEqual(self.lock.read_depth("r1"), 1)
        self.assertEqual(self.lock.waiting(), [("w2", "write")])
        self.assertEqual(self.lock.read_depth("w2"), 0)
        self.assertEqual(self.lock.release_read("w"), 0)
        self.assertEqual(self.lock.release_read("r1"), 0)
        self.assertEqual(self.lock.writer(), "w2")
        self.assertEqual(self.lock.waiting(), [])
        with self.assertRaises(LockError):
            self.lock.downgrade("r1")

    def test_timeout_expires_waiters_behind_an_unbounded_head(self):
        """带超时的等待者到点必须作废，哪怕它排在队首后面。"""
        self.assertTrue(self.lock.acquire_write("w"))
        self.assertFalse(self.lock.acquire_read("r1"))
        self.assertFalse(self.lock.acquire_read("r2", timeout=3))
        self.assertEqual(self.lock.waiting(), [("r1", "read"), ("r2", "read")])
        self.assertEqual(self.lock.advance(3), [("r2", "read")])
        self.assertEqual(self.lock.expired(), [("r2", "read")])
        self.assertEqual(self.lock.waiting(), [("r1", "read")])
        self.assertEqual(self.lock.advance(1), [])
        self.assertEqual(self.lock.expired(), [("r2", "read")])
        self.assertNotIn(("r2", "read"), self.lock.waiting())
        self.assertEqual(self.lock.release_write("w"), 0)
        self.assertEqual(self.lock.read_depth("r1"), 1)
        self.assertEqual(self.lock.read_depth("r2"), 0)
        self.assertEqual(self.lock.waiting(), [])

    def test_release_and_downgrade_reject_misuse(self):
        """释放没持有的锁、降级非写者都要报用法错误。"""
        with self.assertRaises(LockError):
            self.lock.release_read("nobody")
        with self.assertRaises(LockError):
            self.lock.release_write("nobody")
        with self.assertRaises(LockError):
            self.lock.downgrade("nobody")
        self.assertEqual(self.lock.readers(), {})
        self.assertIsNone(self.lock.writer())
        self.assertEqual(self.lock.write_depth(), 0)
        self.assertEqual(self.lock.waiting(), [])
        self.assertEqual(self.lock.expired(), [])
        self.assertTrue(self.lock.acquire_read("r1"))
        self.assertEqual(self.lock.release_read("r1"), 0)
        self.assertEqual(self.lock.readers(), {})
        self.assertTrue(self.lock.acquire_write("w"))
        self.assertEqual(self.lock.release_write("w"), 0)
        self.assertEqual(self.lock.write_depth(), 0)
        with self.assertRaises(LockError):
            self.lock.release_write("w")
        with self.assertRaises(LockError):
            self.lock.release_read("w")


if __name__ == "__main__":
    unittest.main()
