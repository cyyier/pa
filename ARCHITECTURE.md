Pa Language (.pa) — 完整架构与工程规范Slogan: Inspired by Python, 100% Python-syntax compatible. Written by AI, auditable by humans.一、 核心定位与双模式架构哲学Pa (.pa) 是一门 100% 兼容 Python 语法、函数体内零类型噪音、严格遵循 C++ 连续内存布局与 RAII 零 GC 模型、通过 LLVM 编译为原生机器码的系统级编程语言。1. 默认模式：Host-First 混合架构（CLI / 后台服务 / 数据流水线）彻底反转“新语言给 Python 当 C 扩展插件”的传统做法，确立 .pa 统领全局、.py 降级为沙盒工具箱 的主从架构：主宿主脊梁（main.pa & <domain>.pa — Sovereign Spine）：掌控 main() 入口、程序主循环、业务状态机与所有计算密集型逻辑。严禁直接 import 任何第三方 Python 动态库（如 requests、pandas、sqlalchemy）。外部沙盒工具箱（py_workers/<tool>.py — Stateless In -> Out Adapters）：纯粹的无状态辅助函数，仅在需要调用 Python 第三方生态库时由 .pa 通过内置原语 py_call 按需调用。所有动态对象（DataFrame、Response、ORM 实例）必须在 py_workers/ 内部创建、解析并销毁，绝不泄漏到 .pa。批处理铁律（Batching Rule）：严禁在 .pa 的紧凑循环（for / while）内直接调用 py_call。必须在 .pa 侧打包批量输入，单次跨越边界交由 py_workers/ 批处理后返回批量结果。2. 例外模式：🌐 Web / UI 框架控制反转（Frontend-Driven Apps）当项目依赖自带主事件循环的 Python Web/UI 框架（FastAPI、Flask、Django、Streamlit、Gradio）时：不强行用 .pa 包裹事件循环：保留 .py 框架作为极薄的 “HTTP/UI 路由外壳（Thin Router Shell）”，仅负责路由分发、鉴权中间件与前端渲染。100% 业务逻辑下沉 .pa：.py 路由函数内部零业务逻辑，标准三步流水线为：提取请求原始参数 (Clean In) -> 调用 @export 编译的 .pa 领域函数 -> 序列化返回值 (Clean Out) 响应前端。若 .pa 领域逻辑仍需外部库，依旧走 py_workers/ 隔离。二、 边界契约与可调试性铁律（Boundary Contract & Debuggability）为杜绝跨语言静默数据污染与互相甩锅，每一次 .pa $\leftrightarrow$ .py 跨越均执行强制双向安检：双向海关安检（Zero Silent Corruption）：出境安检（.pa -> py_workers）：pa_py_call 跨越前校验所有字段已初始化、无 NaN/Inf（除非显式 @allow_nan）、长度未越界。入境安检（py_workers -> .pa）：所有 py_workers/*.py 函数必须挂载由 AI 自动生成的 @pa_worker_contract(...) 装饰器，返回瞬间立即拦截 None、NaN/Inf 或字段类型漂移。Fail-Fast 黑匣子崩溃报告（pa_boundary_fault.json）：任意边界违例立即熔断，输出包含 direction（PA_TO_PY_IN / PY_TO_PA_OUT）、worker、violation（精确字段路径与非法值）以及 in_payload_snapshot（触发本次调用的完整 In 快照）的 JSON 报告。5 秒二分定责协议（Deterministic Bisection）：每个 .pa 模块与 py_workers/*.py 均配备纯原始类型/JSON 的 In -> Out 固件测试（tests/fixtures/<module>_io.json）。排障时直接拿 in_payload_snapshot 单独重放 py_workers：输入合法但输出错 = 100% .py 工人锅；工人输出全对但终态错 = 100% .pa 核心锅。三、 前端干净子集与编译器内置原语1. 语法白名单与黑名单维度允许（白名单）禁止（黑名单 / 触发自愈工单）类型标注仅在模块级 @struct 字段定义与 @export 跨语言入口处标注；普通函数体内 100% 零类型注解（全靠静态推导）函数体内手写冗余类型注解、Any、Union、Optional数据类型i64(默认整数), i32, i64, u8, f32, f64(默认浮点), bool, void, str_view/bytes_view ({u8*, i64}), 同构定长/动态列表, @struct异构列表 [1, "a", True]、动态 dict、set、tuple内置原语显式注册为 Compiler Intrinsics：py_call、mut(x)、span(arr, start, len)、copy(dst, src, count)、alloc(T, n)、free(p)Python 隐式堆切片 arr[1:5]、列表拼接 a + b、.extend()控制流与可读性if/elif/else、for i in range(...)、while、卫语句提前 return；单函数 ≤ 40 行，圈复杂度 ≤ 8，嵌套 ≤ 3 层；参数 > 2 个必须用关键字参数try/except、yield、闭包、lambda、eval/exec、元类、反射、*args/**kwargs2. C++ 一致拷贝与借用语义值语义赋值（b = a）：对连续数组、@struct、Vec<T> 执行完整深层连续拷贝（llvm.memcpy），b 与 a 内存完全独立（单次栈拷贝超 4KB 发 PA_W001 警告）；禁止 ptr[T] 裸赋值别名（防 Double Free）。默认只读借用（const T& vs T&）：容器与结构体传参默认零拷贝只读借用；函数体内若需修改元素或 .append()，调用处必须显式包裹 mut(arr)。四、 逃逸分析与连续内存分级铁律（Zero Pointer-Chasing, Zero GC）彻底封杀 Python PyObject* 指针数组，所有列表均按三级决策树编译为连续平坦内存：Level 1 — 留在栈上（Stack Allocation，0 堆开销）：所有标量、@struct、编译期定长同构列表 list[T, N]（映射为 LLVM [N x T]，多维嵌套直接打平，总大小 ≤ 64KB）。逃逸分析命中的动态列表（StackBoundedVec[T, MAX_N]）：对 res = [] + res.append(x)，若 res 不逃逸出当前函数且写入次数存在已知常量上界 MAX_N（且 ≤ 64KB），直接在栈上 alloca [MAX_N x T] + 局部计数器 len，.append() 编译为指针偏移写入 + len += 1，零 malloc/free。Level 2 — 连续堆数组 Vec<T>（RAII 确定性释放，0 GC 停顿）：上限不可静态预测、作为返回值逃逸、或超过 64KB 栈阈值的同构动态列表，自动降级为 { T* ptr, i64 len, i64 cap } 连续三元组（初始 cap=16，满容 2 倍 realloc）。未逃逸时在函数所有 return 出口前自动插桩 free(ptr)；逃逸返回时所有权移交调用者作用域负责 free。Level 3 — 红牌拦截，逼 AI 升级重构（Hard Compile Error）：异构列表（[1, "apple", True]） $\rightarrow$ 报 PA_E030，输出 @struct 重构手术单，将二维混合表自动进化为连续结构体数组 Vec<Record>（AoS，缓存命中率 100%）。无法推导元素类型的空列表 $\rightarrow$ 报 PA_E011；局部变量 span 逃逸返回（悬空指针） $\rightarrow$ 报 PA_E040；显式 alloc 漏 free 或 UAF $\rightarrow$ 报 PA_E004。五、 AI 自主审查诊断协议与 VS Code 透视1. --ai-diagnostic 结构化自愈工单编译器报错首要面向 Coding Agent，输出自包含 JSON 工单：必备字段：code、category、severity、skill_rule_ref、location、summary、inferred_context（推导出的底层类型与布局）、causal_chain（从定义到违例的完整因果链）、source_context（带行号上下 3 行源码切片）、repair_plan（含 strategy、anti_patterns_to_avoid 禁止反模式、suggested_patches 确定性替换代码）。2. VS Code LSP 透明透视（pac lsp）Inlay Hints（行内虚拟提示）：在无注解源码旁实时渲染半透明类型与内存策略，如 : list[f64, 16] (128B flat)、: StackVec<i64, max=64> (Zero-Heap)、: Vec<Record> (RAII)、[memcpy 128B]、: const list[f64, 16]&。Hover（鼠标悬浮审计卡片）：悬停展示 LLVM IR 布局、精确字节大小与步长、栈/堆归属、RAII 自动释放行号、函数圈复杂度。六、 三大内置 AI 技能矩阵 (skills/)技能包适用场景核心工作流与门禁pa-coder从头编写新项目按 Host-First（或 Web Router Shell）搭骨架；用 DOD/AoS 连续内存写零注解 .pa；第三方库隔离至 py_workers/ + @pa_worker_contract；跑 pac check --ai-diagnostic 自愈。py-to-pa现有 Python 仓库重构加速先过三色灯熔断门禁（Feasibility Gate）：• 🟢 绿灯：纯计算/无外部依赖 $\rightarrow$ 全量转 .pa。• 🟡 黄灯：含第三方库或 Web 框架 $\rightarrow$ 反转控制权（.pa 宿主 + py_workers/）或保留极薄 Web 路由壳。• 🔴 红灯：重度元编程/动态框架强耦合无法剥离 $\rightarrow$ 立即熔断拒单（STATUS: RED - Keep in .py worker layer + 一句话卡点）。过审后生成双端对拍测试与耗时报告。cpp-to-pa遗留 C++ 安全现代化将 std::array/std::vector/struct/std::span/const T& 剥离宏与裸指针，1:1 无损重构为纯净 .pa，双二进制 Fuzzing 字节级对拍。七、 项目目录结构与两阶段开发路线Plaintextpa/
├── pyproject.toml              # 依赖: llvmlite, pygls, pytest; 入口: pac = "pa_lang.cli:main"
├── ARCHITECTURE.md             # 本文档（项目最高宪法）
├── pa_lang/
│   ├── __init__.py
│   ├── cli.py                  # pac check [--ai-diagnostic] / pac build [-o bin | --shared] / pac lsp
│   ├── types.py                # 连续内存类型系统 & Compiler Intrinsics 注册表
│   ├── infer.py                # 零注解双向类型推导 + 栈上界逃逸分析 + Vec<T> 升级
│   ├── diagnostics.py          # AI 自主审查 JSON 修复工单与 @struct 手术单引擎
│   ├── validator.py            # Host 边界隔离 / 批处理校验 / C++ 拷贝借用 / 可读性铁律
│   ├── codegen.py              # llvmlite IR 生成 (显式声明 malloc/realloc/free/pa_py_call & llvm.memcpy)
│   ├── runtime_bridge.c        # 极简内嵌 CPython 桥接与边界安检运行时
│   └── lsp_server.py           # pygls Inlay Hints + Hover 内存审计 + 实时诊断
├── skills/
│   ├── pa-coder/SKILL.md
│   ├── py-to-pa/SKILL.md
│   └── cpp-to-pa/SKILL.md
├── examples/
│   └── pipeline_demo/
│       ├── main.pa             # Pa 主宿主示例
│       └── py_workers/
│           └── data_fetcher.py # 带 @pa_worker_contract 的沙盒工人
└── tests/
    ├── test_infer.py
    ├── test_validator.py
    └── test_codegen.py
两阶段执行策略（防上下文截断）第一枪（前端与宪法落地）：完成骨架 + types.py + infer.py + diagnostics.py + validator.py + test_infer.py + test_validator.py，确保零注解推导、栈上界分析、PA_E030 手术单、第三方库 import 拦截与循环内 py_call 拦截 100% 绿灯。第二枪（后端、桥接、LSP 与技能包）：完成 codegen.py（顶部显式声明 libc 与 bridge 符号）+ runtime_bridge.c + cli.py（同时支持独立二进制与 --shared 供 Web 路由壳调用）+ lsp_server.py + 三大 SKILL.md + pipeline_demo + test_codegen.py 端到端实测验证。

---

## 修订 R1：`.paw` 跨界区取代 `py_workers/*.py`（本节优先于前文）

| 后缀 | 职责 | 规则 |
|---|---|---|
| `*.pa` | 纯血原生区，100% 编译为 LLVM 机器码 | 禁止 import 第三方 Python 库（`PA_E001`）；只可 import `pa` / `pa_lang` 与同项目的 `.paw`；函数体零类型注解 |
| `*.paw` | 跨界安检区，标准 Python | 可随意 import numpy/pandas/requests；所有公开（非 `_` 开头）函数必须写死静态 In -> Out 签名（`PA_E080`/`PA_E082`），相当于 C 的 `.h` |

- **边界类型**：入参 `i32 i64 u8 f32 f64 bool str_view span[T] list[T,N] Vec[T]`（T 为数值标量）；返回 `标量 | Vec[T] | void`。
- **前端**：`infer.py` 遇到 `import x` 且存在 `x.paw` 时，只解析其顶层函数签名并注册进符号表，`x.f(arr)` 的返回类型静态推导；实参不匹配报 `PA_E090`。
- **后端**：`codegen.py` 不编译 `.paw`；调用点生成封送桩 → `pa_call_paw(mod, fn, args, nargs, ret_tag, ret_elem, &ret)`（`runtime_bridge.c`）。连续内存以 `PyMemoryView_FromMemory` 零拷贝递给 Python。
- **双向安检**：出境查 NaN/Inf/NULL/负长度；入境查 None/NaN/Inf/类型漂移/整数越界/异常；违例写 `pa_boundary_fault.json`（`direction`、`worker`、`violation`、`in_payload_snapshot`）并以退出码 70 熔断。`.paw` 不再需要 `@pa_worker_contract` 装饰器。
- **批处理铁律不变**：循环内调用 `.paw`（或经 helper 间接调用）报 `PA_E002`。
- **LSP**：`.paw` 调用点行内标注 `[🌉 .paw FFI -> f64]`。
- **目录**：`examples/pipeline_demo/{main.pa, data_fetcher.paw}`；构建期把 `sys.base_prefix`、`sys.path` 与 `.paw` 目录烘焙进 `runtime_bridge.o`（可用 `PA_PATH` 环境变量追加 `.paw` 搜索目录）。
- **ABI（`pac build --shared`）**：`@export` 函数以原名导出；标量按值传递，`list[T,N]` 传元素首地址，`Vec[T]` 传指向 `{T*, i64 len, i64 cap}` 的指针，`span[T]` 传 `{T*, i64}` 值。

### 已知限制
- `x = []` 只经 `mut(x)` 传给被调函数时，元素类型取自被调函数里对该形参的 `append`（一层，不跨多层转手）；从未被追加则仍报 `PA_E011`。
- 整数除法 `//` 与 `%` 为 C 语义（向零截断），与 Python 在负数上不同。
- 代码生成器目前不支持：字符串切片、`str` 跨 `.paw` 返回、结构体跨 `.paw` 边界。

### 修订 R2
- `@allow_nan`：装饰 `.paw` 函数后，该函数调用的出境（参数）与入境（返回值）NaN/Inf 安检都被放行；其余检查（None、类型漂移、越界、NULL、负长度）照常。运行时 `allow_nan` 是 `.paw` 内可直接使用的内置装饰器。
- `break` / `continue` 已支持（`for`、`while`）。
- `py-to-pa` 技能新增「负数 `//`、`%` 语义差异」强制提醒。

### 修订 R3
- **默认整数字面量为 `i64`**（原 `i32`）：`range` 循环变量、`int(x)`、整数运算默认 64 位，消除线性同余/时间戳/大累加器的静默溢出。`i32`/`u8` 仅在 `@struct` 字段、`@export` 签名、`.paw` 签名或需要省内存/对齐 C ABI 时显式标注；`i32` 值与 `i64` 混算时提升为 `i64`。注意：`[1, 2, 3]` 现在是 `list[i64, 3]`，传给 `span[i32]` 形参的 `.paw` 会报 `PA_E090`，需改用 `span[i64]` 或显式 i32 容器。

### 修订 R4
- **多维下标** `m[i, j]` ≡ `m[i][j]`（读、写、`+=` 均可），`[[0.0] * W] * H` 生成连续平坦的 `list[list[f64, W], H]`；大数组用循环填充而非展开。
- **字符串**：`str_view` 为只读视图；`a + b`（含链式）与 `s += x` 产生拥有所有权的 `String`（`{u8*, len, cap}`，RAII 自动释放，返回时移交所有权，`b = a` 深拷贝）。支持 `len(s)`、`s[i]`（u8）、`for ch in s`、`==`/`!=`、`print`、作为 `str_view` 参数传给 `.paw`。已知：作为临时值直接传参的拼接结果（如 `f("a" + b)`）不会被释放。
- **`mut()` 元素类型推导**：见上。
- **Web 路由壳**：`examples/web_shell/{stats.pa, app.py}`，`.py` 只做提取参数 -> 调 `@export`（ctypes，`span[f64]` 按 `{ptr, len}` 值传递、`ptr[f64]` 作输出缓冲）-> 序列化；校验错误（422）留在壳里。依赖：`pip install -e .[web]`。

### 修订 R5（以 Skill 硬规矩规避编译器限制）
`pa-coder` 与 `py-to-pa` 技能新增硬规矩：①禁止在调用参数里直接拼接字符串，必须先绑定局部变量再传参（RAII 才能释放）；②空列表跨多层 `mut()` 传递前，须在顶层先确定元素类型或只做单层 `mut()` 填充；③禁止字符串切片；④`.paw` 禁止返回字符串，改用 `Vec[u8]` 字符数组返回。

上述 R5 规矩现已由编译器强制：`PA_E072`（临时 String 只能被赋值给局部、return 或并入同一条拼接链）、`PA_E011`（消息指出 `mut()` 只被转手到哪个被调函数）、`PA_E071`（字符串切片专属提示）、`PA_E082`（`.paw` 返回 `str`/`bytes` 时提示改用 `Vec[u8]`）。

### 修订 R6：dev / prod 双编译模式
`pac check|build --mode dev|prod`（默认 `dev`）。

| | dev | prod |
|---|---|---|
| 编译期 `.paw` 静态检查 | 签名检查 + **深度 In→Out lint**：`PA_E083`（返回 None / 缺 return 路径 / 返回字面量或 `float()`、`int()`、`len()` 等与声明类型不符）、`PA_E084`（写入只读的 `span`/`list`/`Vec` 入参） | 仅签名检查（`PA_E080`/`E082`）及调用点 `PA_E090` 对照 |
| 运行期安检 | 全量：标量 + 缓冲区逐元素 NaN/Inf 扫描（出入境）、None/类型漂移/越界；快照最多 1024 元素 | 轻量：标量 NaN/Inf、None/类型漂移/越界、NULL/长度；**不做缓冲区逐元素扫描**；快照最多 16 元素 |
| `pa_boundary_fault.json` | 含 `"mode": "dev"` | 含 `"mode": "prod"` |

实现：`validator.check_source(mode=)`、`paw.lint_function`、`build.compile_pa(mode=)`；桥接层以 `-DPA_DEEP_CHECKS`、`-DPA_SNAP_LIMIT`、`-DPA_MODE` 在编译 `runtime_bridge.c` 时定型。`@allow_nan` 在两种模式下语义相同。

### 修订 R7：VS Code 本地插件（`vscode-pa/`）
- 注册语言 ID `pa`（`.pa`）与 `paw`（`.paw`）；TextMate 语法继承链 `source.paw → source.pa → source.python`。
- 专属配色：编译器原语 `mut/span/copy/alloc/free/py_call`、装饰器 `@struct/@export/@allow_nan`、类型 `i32 i64 u8 f32 f64 str_view bytes_view void Vec[] ptr[] span[]`。
- 客户端 `src/extension.js` 仅负责启动 `pac lsp`（自动优先 `<workspace>/.venv/bin/pac`，可用 `pa.pacPath` 覆盖）并提供重启 / dev·prod 构建 / `--ai-diagnostic` 检查命令；行内类型提示、悬浮内存审计卡片与诊断均由服务端提供。
- 打包：`npm install && npx @vscode/vsce package --skip-license --allow-missing-repository` 得到 `pa-lang-0.1.0.vsix`。
- 测试：`npm test`（vscode-textmate 实际分词）与 `tests/test_lsp_stdio.py`（真实 LSP JSON-RPC 往返：initialize → didOpen → inlayHint → hover → didChange 诊断 → shutdown）。

### 修订 R8：函数 Out（返回类型）默认写明
- 任何 `.pa` 函数都允许并**默认要求**写 `-> T`（`f64`、`i64`、`Vec[T]`、`list[T, N]`、`void`、`@struct` 名）；形参仍不标注（仅 `@export` / `.paw` 完整标注）。
- 编译器把声明与推导结果对照：不一致报 `PA_E085`（附带按推导结果改写注解的补丁）；数值类型之间不报错，按声明类型做隐式转换（如 `-> f64` 返回整数）；未知类型名同样报 `PA_E085`。声明的返回类型同时用于递归调用与 `@export` ABI。
- 三个 Skill 与示例（benchmark、pipeline_demo）已改为默认写 `-> T`。

### 修订 R9：LSP 自动重启
`pac lsp` 启动后每秒检查 `pa_lang/` 下 `*.py` / `*.c` 的修改时间与大小，一旦变化（去抖 0.5 秒）便正常退出；VS Code 的语言客户端检测到连接关闭会自动拉起新进程，因此改动编译器后编辑器里不再残留旧版诊断。`PA_LSP_AUTORELOAD=0` 可关闭，`PA_LSP_WATCH_DIR` 可改监视目录（测试用）。客户端默认在 3 分钟内最多自动重启 4 次，超出后用 “Pa: Restart Language Server”。

### 修订 R10：默认类型用 Python 叫法
- 别名：`int` ≡ `i64`、`float` ≡ `f64`（注解、`@struct` 字段、`@export` / `.paw` 签名、`list[float, N]`、`Vec[int]`、`span[float]` 均可用）。`i32` / `u8` / `f32` 只在刻意要窄类型时使用。
- 行内虚拟提示：默认宽度显示 Python 名（`: int`、`: float`、`: list[float, 16] (128B flat)`、`: StackVec<int, max=64> (Zero-Heap)`、`[🌉 .paw FFI -> float]`）；窄类型照常显示 `: i32` / `: u8` / `: f32` 作为醒目提醒。
- 悬浮卡片：首行 `int (i64, 8B, stack)` / `list[float, 16] (list[f64, 16], 128B, stack)`，窄类型另加 “narrow type” 提示；其后是 LLVM 布局、字节数与步长、存储位置、RAII `free` 行号、圈复杂度。
- 诊断修复补丁（`PA_E085` 等）中的注解同样以 Python 名拼写。

### 修订 R11：默认提示再减负 + 单行注释硬规矩
- **行内提示分两档**（`pa.inlayHints.detail`，默认 `simple`；经 LSP `initializationOptions.hintDetail` 传给服务端，切换设置会自动重启服务）：
  - `simple`（默认）：只写 Python 名，不带尺寸/存储类：`: int`、`: float`、`: bool`、`: str`、`: list[float]`（定长数组、栈上向量、堆 Vec 一律 `list[...]`）、`: 结构体名`、`[🌉 .paw → float]`；窄类型依然显形（`: i32`、`: u8`、`: list[f32]`）。`[free: RAII]`、`[memcpy ..]` 不再显示（>4KB 拷贝仍有 `PA_W001`）。
  - `detailed`：`: list[float, 16] (128B flat)`、`: StackVec<int, max=64> (Zero-Heap)`、`: Vec<int> (RAII)`、`[free: RAII]`、`[memcpy 128B]`、`[🌉 .paw FFI -> float]`。
  - 内存细节统一放进悬浮卡片（`int (i64, 8B, stack)`、LLVM 布局、步长、RAII 释放行、复杂度）。
- **Skill 硬规矩**：凡不好理解或操作复杂的函数（模/位/溢出敏感运算与技巧算法、手动内存 `alloc/free/span/copy/mut()` 转手、`.paw` 桥接与批处理、刻意的窄类型、圈复杂度 ≥ 5 等），`def` 正上方必须有且仅有一行 `# …` 注释（≤ 100 字符，写意图/陷阱而非复述代码）；平凡函数不写。三个 Skill 与示例已按此执行。

### 修订 R12：边界条件加固（全部 P0 + 3 条 P1）
| 项 | 处理 |
|---|---|
| 整数 `//` `%` 除零 | 两种模式都检查，`ZeroDivisionError`；`INT_MIN // -1` 不再陷阱。浮点除零仍按 IEEE 得 `inf`/`nan`（与 Python 不同，已写入 Skill） |
| 下标越界 / 负下标 | **负下标视为越界，不折算 `len + i`**。dev 模式对每个变量下标（含 `m[i, j]` 逐维、`span`、`str`）做无符号 `idx < len` 检查，违例 `IndexError`；prod 不检查。常量负下标 `PA_E073`，常量下标越过定长数组为编译错误 |
| `[x] * N` 超过 64KB | 自动落到堆上 `Vec`（RAII 释放，可 `return`），二维同理 |
| 变量类型变化 | `PA_E086`（含定长数组长度变化），不再让 LLVM 崩溃 |
| `range` 变量步长 | 进入循环前判断：`step == 0` → `ValueError`，`> 0` 递增，`< 0` 递减；常量 0 为编译错误 |
| 遍历时修改同一列表 | `PA_E087`（`append` / `mut()` / 重新绑定） |
| 可能未赋值就使用 | `PA_E088`（按 if/else 交集、循环体不计入的定值分析） |
| 别名 `f(v, mut(v))` | `PA_E089`（同一调用里同一变量既 `mut()` 又再次出现） |
| 窄类型截断 | 传给 `.paw` 的 `u8`/`i32` 参数始终做范围检查（`OverflowError`），不适配的字面量编译期报错；dev 模式下赋值/传参/结构体字段也检查；浮点不再允许传给整数形参（`PA_E090`） |
| 模块级常量 | 支持 `int` / `float` / `bool` / `str` 及其常量算术 |
| `print` | 与 Python `repr` 对齐（最短往返浮点、`15.0`、`1e+20`、`True`） |
| 共享库不再杀宿主 | `@export` 函数由 C 包装器（`setjmp` 守卫）调用；故障时返回 0 并记录错误，宿主用 `pa_error_code()` / `pa_error_message()` / `pa_error_json()` / `pa_error_clear()` 读取；`.paw` 桥接故障先释放 GIL 再回跳。FastAPI 示例把故障转成 HTTP 400 |
| `@allow_nan` 竞态 | 标志改为按调用传参，不再使用全局变量 |
| 运行时 | 新增 `runtime_core.c`（无 Python 依赖、始终链接）：故障、守卫、print 助手；运行时故障写 `pa_runtime_fault.json`（含 `mode`、`kind`、`detail`、`function`、`line`） |

仍搁置的 P2：绝对路径烘焙、冷启动、`pa_boundary_fault.json` 并发覆盖、栈上向量的深递归栈溢出（待发版/部署阶段处理）。
