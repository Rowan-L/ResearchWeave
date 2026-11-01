"""B22 单实例防护测试:同 key 第二实例 acquire 失败并激活既有实例;异 key 不干扰。"""

from synthrecall.single_instance import SingleInstanceGuard, instance_key


def test_instance_key_is_user_scoped():
    key = instance_key("SynthRecall")
    assert key.startswith("SynthRecall-single-instance-")
    assert instance_key("SynthRecall") == key, "同应用同用户 key 稳定"


def test_second_instance_acquire_fails_and_signals_first(qtbot):
    key = instance_key("SynthRecall-test")
    first = SingleInstanceGuard(key)
    assert first.acquire(), "首个实例应成功占用通道"
    try:
        second = SingleInstanceGuard(key)
        with qtbot.waitSignal(first.raiseRequested, timeout=3000):
            assert not second.acquire(), "第二实例应 acquire 失败(已通知既有实例)"
    finally:
        first.release()


def test_different_keys_do_not_interfere(qtbot):
    first = SingleInstanceGuard(instance_key("SynthRecall-a"))
    other = SingleInstanceGuard(instance_key("SynthRecall-b"))
    assert first.acquire()
    try:
        assert other.acquire(), "不同 key 是不同应用实例,互不干扰"
    finally:
        first.release()
        other.release()


def test_release_allows_next_instance(qtbot):
    key = instance_key("SynthRecall-test-release")
    first = SingleInstanceGuard(key)
    assert first.acquire()
    first.release()
    second = SingleInstanceGuard(key)
    assert second.acquire(), "释放后可再次占用(正常退出后重启场景)"
    second.release()
