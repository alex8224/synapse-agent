---
name: python-memory-diagnostics
description: 诊断 Python 进程内存持续增长，区分对象泄漏、缓存保留与 RSS 高水位；适用于 Textual/Rich TUI 的窗口切换、组件卸载和渲染缓存问题。
license: MIT
compatibility: Requires Python 3.12+; project commands use uv and pytest.
metadata:
  version: "1.0.0"
  owner: coding-agent
allowed-tools: execute read_file write_file edit_file
---

# Python 内存增长诊断

## 使用时机

当用户报告以下现象时使用本 Skill：

- 重复打开、关闭窗口后内存持续增加。
- 切换文件、页面或组件时 RSS 上升，关闭后不下降。
- 怀疑 Textual、Rich、语法高亮、diff 组件或第三方 Widget 泄漏。
- `gc.collect()` 后进程内存仍不回落。
- 需要判断问题是对象泄漏、全局缓存、原生内存，还是解释器高水位。

## 核心原则

1. **RSS 不下降不等于对象泄漏。** Python 分配器、C 扩展和操作系统可能保留已释放内存页供进程复用。
2. **先证明对象是否仍存活，再寻找引用者。** 使用弱引用和对象计数，不要只看任务管理器。
3. **同时观察三层数据。** 进程 RSS、`tracemalloc` 跟踪堆、关键对象存活情况缺一不可。
4. **看多轮增长斜率，不看单次峰值。** 首轮导入、字体/样式、lexer 和渲染缓存预热通常会增加内存。
5. **`gc.collect()` 是诊断手段，不是默认修复。** 它不能清除仍被引用的对象，也不保证 RSS 下降。
6. **依赖库问题直接读源码。** 重点检查缓存装饰器、模块全局变量、实例方法缓存和生命周期回调。
7. **修复后同时验证功能、生命周期和内存。** 内存修复不能破坏关闭、异步任务、键盘操作或返回值契约。

## 诊断流程

### 1. 固定可重复场景

先把用户操作写成确定的循环：

1. 启动应用并完成一次预热。
2. 记录预热后的基线。
3. 打开目标窗口。
4. 重复切换文件或刷新内容固定次数。
5. 关闭窗口并等待卸载、worker 和消息队列稳定。
6. 执行 `gc.collect()`，再次采样。
7. 重复整个开关周期至少 3 次。

测试数据、终端尺寸、切换顺序和等待时间应保持一致。异步 TUI 中应显式等待界面稳定，例如 `await pilot.pause()`；必要时等待两次事件循环 tick。

不要把进程刚启动时的数据当基线。先预热导入、主题、语法高亮和首轮渲染。

### 2. 同时采集三类证据

| 层级 | 工具 | 能回答的问题 | 不能证明的事情 |
|---|---|---|---|
| 进程内存 | RSS / Private Bytes | 操作系统看到的进程占用是否持续增长 | 哪个 Python 对象仍存活 |
| Python 分配 | `tracemalloc` | 哪些 Python 调用栈新增了已跟踪分配 | C 扩展、终端驱动和全部原生内存 |
| 对象生命周期 | `weakref`、`gc.get_objects()` | 窗口、Widget、payload 是否在关闭后仍存活 | 已释放内存是否归还操作系统 |

推荐的最小探针：

```python
from __future__ import annotations

import gc
import tracemalloc
import weakref
from collections.abc import Awaitable, Callable
from typing import Any


def tracked_mib() -> tuple[float, float]:
    current, peak = tracemalloc.get_traced_memory()
    mib = 1024 * 1024
    return current / mib, peak / mib


def live_count(cls: type[Any]) -> int:
    # 不创建对象列表，避免探针本身延长对象生命周期。
    return sum(1 for obj in gc.get_objects() if isinstance(obj, cls))


def checkpoint(label: str, *classes: type[Any]) -> None:
    gc.collect()
    current, peak = tracked_mib()
    counts = ", ".join(f"{cls.__name__}={live_count(cls)}" for cls in classes)
    print(f"{label}: traced={current:.2f} MiB peak={peak:.2f} MiB {counts}")


async def exercise_cycle(
    open_target: Callable[[], Awaitable[Any]],
    exercise_target: Callable[[Any], Awaitable[None]],
    close_target: Callable[[Any], Awaitable[None]],
    settle_ui: Callable[[], Awaitable[None]],
) -> weakref.ReferenceType[Any]:
    target = await open_target()
    target_ref = weakref.ref(target)
    await exercise_target(target)
    await close_target(target)

    # 清掉探针自己的强引用，再等待框架完成卸载。
    target = None
    await settle_ui()
    gc.collect()
    return target_ref
```

项目默认命令：

```bash
uv run python path/to/memory_probe.py
```

如果环境已有 `psutil`，可补充 RSS；不要仅为一次诊断立即新增生产依赖：

```python
import os
import psutil

rss_mib = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
```

Windows 也可以从外部观察指定 PID：

```powershell
Get-Process -Id <PID> |
  Select-Object Id,
    @{Name='RSSMiB'; Expression={[math]::Round($_.WorkingSet64 / 1MB, 2)}},
    @{Name='PrivateMiB'; Expression={[math]::Round($_.PrivateMemorySize64 / 1MB, 2)}}
```

### 3. 正确解释结果

| 观测结果 | 优先判断 | 下一步 |
|---|---|---|
| 关键对象弱引用仍存活，数量每轮增加 | 真实引用泄漏 | 查引用链、任务、回调、容器和缓存 |
| 弱引用消失，但 `tracemalloc` current 每轮增加 | 其他 Python 对象或全局缓存保留 | 比较快照，定位新增分配栈 |
| 弱引用消失，tracked heap 回到基线附近，但 RSS 不降 | 分配器或原生内存高水位 | 继续多轮验证是否平台化，不要误判泄漏 |
| 前几轮上涨，随后稳定 | 有界缓存或一次性预热 | 核对缓存上限和稳定平台是否可接受 |
| 每轮近似线性上涨，无平台 | 无界缓存或持续泄漏 | 做 A/B 隔离并查增长斜率最大的路径 |
| 关闭后对象最终消失，但短时间仍存活 | 异步卸载或延迟任务 | 增加稳定等待，检查 worker/timer 是否按期结束 |

判断“无持续泄漏”时至少需要以下两项证据：

- 关闭后目标窗口或 Widget 的弱引用消失。
- 多轮循环后关键对象数量不累积。
- `tracemalloc` current 回到稳定区间或出现明确平台。
- RSS 在预热后不再按轮次近似线性增长。

### 4. 用 `tracemalloc` 定位分配来源

在预热并 GC 后记录基线快照，在多轮操作和关闭后记录第二个快照：

```python
import gc
import linecache
import tracemalloc

tracemalloc.start(25)
await warmup()
gc.collect()
before = tracemalloc.take_snapshot()

for _ in range(10):
    await run_one_cycle()

gc.collect()
after = tracemalloc.take_snapshot()

for stat in after.compare_to(before, "traceback")[:20]:
    frame = stat.traceback[0]
    source = linecache.getline(frame.filename, frame.lineno).strip()
    print(f"{stat.size_diff / 1024:.1f} KiB {stat.count_diff:+d}")
    print(f"  {frame.filename}:{frame.lineno}: {source}")
```

分析时：

- 先看 `size_diff` 和 `count_diff` 均持续为正的调用栈。
- 同时比较 `lineno` 和 `traceback`，后者更容易发现调用链。
- 分别标记项目源码、Python 标准库、Textual/Rich 和第三方组件。
- 快照比较必须放在相同生命周期点，例如都在关闭并 GC 后。
- 不要把峰值 `peak` 当成当前泄漏量；主要看 `current` 和快照差异。

### 5. 用弱引用确认生命周期

对窗口、重型 Widget、payload 和后台任务分别建立弱引用：

```python
import gc
import weakref

screen = app.screen
screen_ref = weakref.ref(screen)

await close_screen()
screen = None
await pilot.pause()
await pilot.pause()
gc.collect()

assert screen_ref() is None
```

注意：

- 局部变量、异常 traceback、测试 fixture、闭包和调试器都可能保留对象。
- 不要把目标对象放入普通列表；只保存 `weakref.ref`。
- Textual 的 screen stack、worker、timer 和消息队列可能需要额外 tick 才完成清理。
- 如果对象仍存活，再使用 `gc.get_referrers()`；不要一开始就打印完整对象图。

只汇总引用者类型，避免探针制造大量新引用：

```python
from collections import Counter
import gc

victim = target_ref()
if victim is not None:
    referrer_types = Counter(type(obj).__qualname__ for obj in gc.get_referrers(victim))
    print(referrer_types.most_common(20))
del victim
```

`gc.get_referrers()` 会受到当前栈帧和调试代码影响，其结果只能作为线索，必须结合源码确认。

### 6. 做最小 A/B 隔离实验

一次只替换一个变量，并比较每轮增长斜率：

| 实验 | A | B | 可验证的假设 |
|---|---|---|---|
| 内容负载 | 空内容 | 真实大文件 | 是否由数据量或高亮结果驱动 |
| 组件类型 | 内置 `Static` | 第三方重型 Widget | 是否由组件实现或其缓存驱动 |
| 生命周期 | 复用单个 Widget 并 `update()` | 每次 remove/mount 新 Widget | 是否由挂载 churn 驱动 |
| 渲染方式 | 轻量 Rich `Text` | Syntax/diff 高亮组件 | 是否由渲染缓存驱动 |
| 窗口行为 | 只打开关闭 | 打开后重复切换 | 泄漏发生在窗口生命周期还是内容切换 |
| 缓存行为 | 默认缓存 | 诊断性清空缓存 | 增长是否由某个缓存保留 |

优先使用最小组件复现，不要同时重写业务逻辑、样式和异步流程。只有 A/B 结果明确后才实施正式修复。

## 依赖源码检查清单

定位到第三方库后，直接读取当前安装版本源码，不依赖猜测。项目使用 `uv`：

```bash
uv run python -c "import inspect, textual; print(textual.__version__); print(inspect.getfile(textual))"
uv run python -c "import inspect; from textual._styles_cache import StylesCache; print(inspect.getsource(StylesCache.get_inner_outer))"
```

重点搜索：

- `@functools.lru_cache`、`@cache`、自定义 LRU。
- 模块级 `dict`、`set`、列表和单例 registry。
- 实例方法上的缓存装饰器。
- class attribute 中保存的 Widget、renderable 或 callback。
- timer、worker、async task、watcher、事件订阅和闭包。
- 高亮行、opcode、`Strip`、`Segment`、`Text`、`Syntax` 的缓存。
- 卸载后仍保存父子节点、screen 或 app 的键值。

### 实例方法 LRU 的关键风险

实例方法的缓存 key 通常包含 `self`：

```python
from functools import lru_cache

class Renderer:
    @lru_cache(maxsize=1024)
    def render(self, width: int):
        ...
```

只要缓存项未淘汰，缓存就会强引用 `self`。如果 `self` 又引用 Widget 或渲染结果，已卸载组件可能继续存活。检查：

```python
print(Renderer.render.cache_info())
Renderer.render.cache_clear()  # 仅用于有证据的诊断或受控清理
```

不要看到 `lru_cache` 就直接判定为 bug。应先验证：

1. 缓存命中/大小是否随操作增长。
2. 清空缓存后弱引用或 tracked heap 是否发生预期变化。
3. 缓存是否有明确上限并最终平台化。
4. 清空是否会影响其他仍在显示的组件。

## Textual/Rich 专项检查

### 生命周期

- 关闭前取消或失效化仍在运行的 worker、timer 和异步回调。
- 使用 generation/token 防止旧任务把结果写回已关闭页面。
- 清空大 payload、renderable 和当前内容引用。
- 等待 `remove_children()`、`dismiss()`、`pop_screen()` 真正完成。
- 覆盖框架方法时保留其返回契约，例如 `dismiss()` 的返回值。
- 新增状态字段前检查基类同名字段，避免覆盖 Textual 内部状态。

特别注意：不要使用 `_closed` 之类常见内部名作为业务标志。Textual 的消息泵自身可能使用该字段；覆盖后可能使 `Prune` 等生命周期消息被丢弃，造成关闭卡住。应使用明确且命名空间化的字段，例如 `_dismiss_started`。

### Widget 与渲染

- 优先复用一个稳定的子 Widget，通过 `Static.update()` 更新内容。
- 避免每次文件切换都 remove/mount 一个重型 diff 或高亮 Widget。
- 大文本优先构造一次轻量 Rich renderable，不要保存重复的逐行高亮副本。
- 检查第三方 diff Widget 是否同时缓存原文、opcode、高亮行和样式字典。
- 区分“Widget 已被 GC”与“框架全局样式缓存仍保留渲染结果”两种情况。

### Textual 样式缓存模式

若 `tracemalloc` 明确指向 Textual 样式缓存，并且 A/B 实验证明卸载组件仍被实例键缓存保留，可检查：

```python
from textual._styles_cache import StylesCache

info = StylesCache.get_inner_outer.cache_info()
print(info)
```

受控清理示例：

```python
def clear_textual_style_cache_refs() -> None:
    try:
        from textual._styles_cache import StylesCache

        cache_clear = getattr(StylesCache.get_inner_outer, "cache_clear", None)
        if callable(cache_clear):
            cache_clear()
    except Exception:
        # 私有 API 可能随 Textual 版本变化；清理不能阻断正常关闭。
        pass
```

这是第三方私有 API，只有在以下条件均满足时才使用：

- 快照和对照实验已经指向该缓存。
- 当前安装版本源码确认它是实例键缓存。
- 清理发生在受控生命周期点，例如目标窗口卸载后。
- 已测试不会破坏其他可见 Widget 的渲染。
- 代码采用 best-effort 兼容，并有回归测试覆盖关闭流程。

全局 `cache_clear()` 会影响整个进程的缓存命中率。长期更稳妥的方案通常是减少重型 Widget churn、复用组件，或升级到已修复该问题的依赖版本。

## 修复优先级

按以下顺序选择最小、安全的修复：

1. **修复所有权。** 移除应用自身列表、字典、闭包、任务或回调中的强引用。
2. **完成生命周期。** 取消 worker/timer，失效化晚到结果，清空大 payload。
3. **复用组件。** 用一个稳定 Widget 更新内容，避免反复挂载重型组件。
4. **限制应用缓存。** 设置合理 `maxsize`，按窗口或文档粒度淘汰。
5. **降低渲染分配。** 用轻量 Rich 渲染替代重复高亮和多层缓存。
6. **升级或修补依赖。** 先确认当前版本源码和上游行为。
7. **受控清理第三方缓存。** 仅在证据充分且无公开 API 时作为兼容措施。
8. **最后才考虑显式 GC。** 它只能加速回收不可达循环，不能修复强引用泄漏。

不要通过随意把第三方对象私有属性设为 `None` 来“修复”泄漏。除非已读源码并覆盖所有不变量，否则容易造成卸载异常或关闭冻结。

## 回归验证

先运行最窄测试，再扩大范围：

```bash
uv run pytest tests/test_target.py -k memory_case -q
uv run pytest tests/test_target.py -q
uv run ruff check .
```

至少验证：

| 类别 | 验证项 |
|---|---|
| 功能 | 切换、刷新、split/unified、annotation 等原功能正常 |
| 关闭 | Esc、按钮关闭和程序化 dismiss 均不会卡住 |
| 异步 | 关闭后晚到 worker 不再更新已卸载 Widget |
| 对象 | 关闭并稳定后窗口/重型 Widget 弱引用消失 |
| 数量 | 多轮后关键类实例数不随轮次累积 |
| Python 堆 | 预热后 `tracemalloc` current 回到稳定区间 |
| RSS | 多轮后不再线性增长；不要求每次关闭立即回到启动值 |
| 缓存 | 缓存大小有界，或在明确生命周期点得到释放 |

### 测试断言建议

推荐断言对象生命周期和增长上限：

```python
assert screen_ref() is None
assert live_count(TargetWidget) <= baseline_count + tolerance
assert final_traced_mib <= baseline_traced_mib + allowed_growth_mib
```

避免把“关闭后 RSS 必须下降到精确值”写入单元测试。RSS 受解释器分配器、平台和测试顺序影响，容易产生假失败。RSS 更适合作为重复探针中的趋势指标。

## 常见误区

- 只看任务管理器，看到 RSS 不降就宣布泄漏。
- 未预热就在首次操作前后比较。
- 只运行一轮，没有观察增长是否平台化。
- 在对象仍有局部变量引用时检查弱引用。
- 用 `del` 或 `gc.collect()` 代替查找所有权。
- 只清理组件内部缓存，却忽略框架级或模块级缓存。
- 快照采样点生命周期不一致。
- 探针把目标对象放进列表、异常或日志结构，反过来制造泄漏。
- 未读依赖源码就修改第三方私有字段。
- 为了降 RSS 牺牲关闭语义、异步安全或返回值契约。

## 本项目案例模式

本项目 Git Explore 问题提供了一个可复用的判断模式：

1. 旧 diff Widget 的弱引用能够消失，对象数量没有持续累积，因此不能直接认定 Widget 本身泄漏。
2. `tracemalloc` 将持续保留定位到 Textual 的实例键样式 LRU，而不是业务 payload 列表。
3. 样式缓存保存已卸载 Widget 对应的 `Strip` 等渲染结果；RSS 同时包含解释器/渲染器高水位。
4. 对照实验显示复用单个 `Static` 并使用轻量 Rich 渲染显著降低切换分配。
5. 在窗口卸载的受控位置清理已证实的样式缓存引用，并验证关闭流程不冻结。
6. 生命周期字段使用 `_dismiss_started`，避免覆盖 Textual `MessagePump._closed`。

这个案例的通用结论不是“所有 Textual 内存问题都清缓存”，而是：

> 先用弱引用排除对象泄漏，再用 `tracemalloc` 和 A/B 实验定位缓存所有者；只有证据指向具体缓存时，才选择复用组件、降低渲染分配或受控清理。

## 最终报告模板

```text
结论：<真实泄漏 / 有界缓存 / RSS 高水位 / 混合问题>

证据：
- 场景：预热后执行 <N> 轮，每轮 <操作>
- RSS：<基线、峰值、关闭后、每轮斜率>
- tracemalloc：<current/peak/主要增长调用栈>
- 对象：<弱引用结果、关键类计数>
- A/B：<最小组件与真实组件的差异>

根因：
- <具体所有者、缓存、任务或调用链>

修复：
- <最小改动及其生命周期位置>

验证：
- <测试命令和结果>
- <多轮探针结果>

剩余风险：
- <私有 API、平台差异、允许的高水位或依赖升级事项>
```
